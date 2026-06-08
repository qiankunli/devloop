#!/usr/bin/env python3
"""gcam / gcamp / gcampr orchestrator — the one place the commit→push→MR flow lives.

Routes ALL git through `gitcmd` and ALL code-review hosting through the `lib.forge` facade
(GitHub / GitLab picked per-repo) — no scattered subprocess/urllib. Skills call this
(via smart_gcamp.sh / smart_gcampr.sh)
rather than raw git, so the AI never issues raw `git commit/push` (which the PreToolUse
guards would otherwise intercept), and the decision logic lives here, not in markdown.

Modes:
  commit  → stage + commit (gcam)
  push    → stage + commit + push (gcamp)
  mr      → stage + commit + push + create/reuse PR/MR (gcampr)

Branch positioning is decided from explicit intent, NOT from whichever branch HEAD happens
to sit on (a prior gcampr leaves us on the branch it just created — building the next branch
off that would silently bleed its commits into the new MR):
  --branch <name>  → cut a FRESH branch off origin/<target> (or off --base for intentional
                     stacking); outcome is the same no matter where HEAD currently is.
  no --branch      → commit onto the current branch (continue its MR); refused if that branch
                     is protected or its MR is merged/closed (stale) — pass --branch instead.
A fresh-cut branch is then asserted to carry only this run's commit(s) before push/MR, so a
mis-based branch is caught at creation rather than at merge. Emits a self-narrating PLAN banner.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib import git_state, gitcmd, repo_resolve  # noqa: E402
from lib.context import RepoContext, record_active_repo  # noqa: E402
from lib.forge import ForgeError, forge_for_repo  # noqa: E402


class SmartError(Exception):
    pass


# ── phase dataflow ──────────────────────────────────────────────────────────────
# main() = resolve_intent → prepare_branch → stage_and_commit → publish.
# Each phase is a function of (intent, plan) + its predecessor's result — no phase
# re-reads argv/cwd/git defaults, and no cross-phase mutable locals (the old
# `current = ns.branch` style). New flow steps (amend-MR, validation gate…) slot in
# as new phases instead of growing main().


@dataclass(frozen=True)
class GitIntent:
    """One run's full intent, resolved ONCE from argv + state.

    `explicit_base` is kept apart from `base`: an explicit --base opts into stacking
    and disables the foreign-commit guard; the default origin/<target> base enables it.
    """
    mode: str
    message: str
    title: str
    requested_branch: str | None
    target: str
    base: str
    explicit_base: bool
    files: list[str]
    repo: str
    source: str       # repo resolution self-narration, goes into the PLAN banner
    invoke_cwd: str


@dataclass(frozen=True)
class BranchResult:
    branch: str   # the branch this run commits on, after positioning
    cut: bool     # freshly cut this run → foreign-commit guard applies


@dataclass(frozen=True)
class StageResult:
    committed: bool


def run(repo: str, *args: str, timeout: int = 30) -> str:
    r = gitcmd.git(repo, *args, timeout=timeout)
    if not r.ok:
        raise SmartError(f"git {' '.join(args)} failed: {r.err or r.out}")
    return r.out


def cut_new_branch(repo: str, name: str, base: str, plan: list[str]) -> None:
    # `base` is the ref the new branch is cut from: `origin/<target>` by default (a fresh,
    # independent feature) or an explicit `--base` ref for intentional stacking. Fetch first
    # when cutting off a remote-tracking ref so we branch off the latest tip, not a stale local copy.
    if base.startswith("origin/"):
        gitcmd.git(repo, "fetch", "origin", base.split("/", 1)[1], timeout=30)
    r = gitcmd.git(repo, "checkout", "-b", name, base)
    if not r.ok:
        raise SmartError(
            f"could not cut '{name}' off {base}: {r.err}\n"
            "If you have conflicting uncommitted changes, stash them first."
        )
    plan.append(f"cut new branch '{name}' off {base}")


def decide_branch(
    current: str | None,
    requested: str | None,
    *,
    protected: bool,
    stale: bool,
    base: str,
) -> tuple[str, str | None]:
    """Decide how to position the working branch — from explicit intent, NOT from whichever
    branch HEAD happens to sit on.

    `--branch` always means "fresh branch off `base`" (origin/<target> by default), so the
    outcome never depends on a prior gcampr having left us on an unrelated open/unmerged feature
    branch — the footgun where the new MR silently inherits that branch's commits. `--base` opts
    into intentional stacking. No `--branch` continues the current branch.

      ("cut", base)      → cut `requested` off `base`
      ("continue", None) → commit onto the current branch (continue its MR)
      ("error", why)     → refuse (current is protected/stale and no --branch given); caller raises
    """
    if requested and requested != current:
        return ("cut", base)
    if protected or stale:
        return ("error", "protected branch" if protected else "current branch's MR is merged/closed")
    return ("continue", None)


_SENSITIVE_BASENAMES = {".env", ".DS_Store"}
_SENSITIVE_DIRS = {".idea", ".vscode", "__pycache__", ".devloop"}


def _is_sensitive(path: str) -> bool:
    parts = path.split("/")
    if parts[-1] in _SENSITIVE_BASENAMES or parts[-1].startswith(".env"):
        return True
    return any(p in _SENSITIVE_DIRS for p in parts)


def normalize_files(repo: str, files: list[str], invoke_cwd: str | Path, plan: list[str]) -> list[str]:
    """Rebase explicit --files entries onto repo-root-relative paths.

    `git add` runs at the repo root, but callers pass paths relative to wherever they
    happened to run (the workspace root, a server/ code dir, ...) — a mis-based path
    used to die with a raw `git add` error. Rewrite absolute paths and paths that exist
    relative to the invoking cwd (but not the repo root); leave the rest untouched
    (e.g. a deletion staged by name, which exists nowhere).
    """
    repo_real = Path(repo).resolve()
    out: list[str] = []
    for f in files:
        p = Path(os.path.expanduser(f))
        rebased: Path | None = None
        if p.is_absolute():
            try:
                rebased = p.resolve().relative_to(repo_real)
            except ValueError:
                pass   # outside the repo — let git report it
        elif not (repo_real / f).exists():
            cand = Path(invoke_cwd) / f
            if cand.exists():
                try:
                    rebased = cand.resolve().relative_to(repo_real)
                except ValueError:
                    pass
        if rebased is not None and str(rebased) != f:
            plan.append(f"rebased --files path: {f} → {rebased}")
            out.append(str(rebased))
        else:
            out.append(f)
    return out


def stage(repo: str, files: list[str], plan: list[str]) -> None:
    if files:
        # Explicit list still honors the sensitive blocklist (a named dir could pull in
        # .env/__pycache__; symmetry with implicit staging). Transparent in the PLAN.
        to_add = []
        for f in files:
            if _is_sensitive(f):
                plan.append(f"skipped sensitive (explicit): {f}")
                continue
            to_add.append(f)
    else:
        # Stage modified + NEW (untracked) files that git isn't ignoring, minus a small
        # sensitive blocklist. `git add -u` would miss new files (a dev-loop creates them
        # constantly); `git add -A` is too broad (the guard blocks the AI from it). This is
        # the controlled middle: `status --porcelain` already excludes ignored files.
        out = gitcmd.git(repo, "-c", "core.quotepath=false", "status", "--porcelain").out
        to_add = []
        for line in out.splitlines():
            # Split status-code from path on whitespace (NOT fixed columns): gitcmd
            # strips output, which eats the leading space of an unstaged-modified
            # first line, so column slicing would drop a char.
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            path = parts[1]
            if " -> " in path:                      # rename: stage the new path
                path = path.split(" -> ", 1)[1]
            path = path.strip().strip('"')
            if not path:
                continue
            if _is_sensitive(path):
                plan.append(f"skipped sensitive: {path}")
                continue
            to_add.append(path)
    if to_add:
        run(repo, "add", "--", *to_add)
    shown = ", ".join(to_add[:8]) + (" …" if len(to_add) > 8 else "")
    plan.append(f"staged {len(to_add)} file(s): {shown}" if to_add else "nothing to stage")
    # Safety: refuse an accidental embedded-repo gitlink (mode 160000) in the index.
    raw = gitcmd.git(repo, "diff", "--cached", "--raw").out
    gitlinks, real = [], []
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        (gitlinks if (meta.startswith(":160000") or " 160000 " in meta) else real).append(path)
    if gitlinks:
        gitcmd.git(repo, "reset", "-q")
        # Hand back the safe retry instead of making the caller re-enumerate by hand.
        retry = f"\nRetry with the real files only: --files {','.join(real)}" if real else ""
        raise SmartError(
            f"staging captured a submodule/embedded-repo gitlink ({', '.join(gitlinks)}) — unstaged.{retry}"
        )


_VERSION_BASENAMES = {"pyproject.toml", "package.json", "plugin.json", "VERSION", "version.py", "__version__.py"}
_VERSION_LINE_RE = re.compile(r'^[+-]\s*"?version"?\s*[:=]', re.IGNORECASE)


def warn_mixed_version_bump(repo: str, plan: list[str]) -> None:
    """Soft hint (a PLAN line, never a block): a version bump riding along with feature
    files is the 'tangled bump needs a manual discard later' friction — flag it so the
    caller can split with --files when the bump is unrelated. Amend/release commits
    that intentionally pair them just read past the note."""
    staged = gitcmd.git(repo, "diff", "--cached", "--name-only").out.splitlines()
    vfiles = [f for f in staged if Path(f).name in _VERSION_BASENAMES]
    others = [f for f in staged if Path(f).name not in _VERSION_BASENAMES]
    if not vfiles or not others:
        return
    bumped = [
        f for f in vfiles
        if any(_VERSION_LINE_RE.match(line)
               for line in gitcmd.git(repo, "diff", "--cached", "-U0", "--", f).out.splitlines())
    ]
    if bumped:
        plan.append(
            f"note: version bump in {', '.join(bumped)} is mixed with {len(others)} other file(s) — "
            "if the bump is unrelated, split it out with --files"
        )


def reuse_or_create_pr(repo: str, source: str, target: str, title: str, plan: list[str]):
    """Open a PR/MR for `source`→`target`, reusing the branch's open one if present.
    Provider-neutral: the forge is resolved from the repo's origin (GitHub or GitLab)."""
    forge = forge_for_repo(repo)
    if forge is None:
        raise SmartError("branch pushed, but no token / unsupported remote — open the PR/MR manually.")
    try:
        existing = forge.for_branch(source)
        if existing and existing.is_open:
            plan.append(f"reused open {existing.label}")
            return existing
        pr = forge.create(source_branch=source, target_branch=target, title=title)
        plan.append(f"created {pr.label}")
        return pr
    except ForgeError as e:
        raise SmartError(f"branch pushed, but PR/MR create/reuse failed: {e}")


def resolve_intent(ns: argparse.Namespace, invoke_cwd: str) -> GitIntent:
    """Phase 1: argv + state → one immutable intent.

    cwd-independent: the session cwd routinely sits at the aggregate workspace root,
    so the repo is resolved from intent/state (ResolvedRepo) instead of dying on
    "not a git repo". Raises SmartError when no repo can be resolved.
    """
    resolved, how = repo_resolve.resolve_repo_dir(ns.repo, invoke_cwd)
    if not resolved:
        raise SmartError(how)
    record_active_repo(resolved.git_root)
    target = ns.target or git_state.get_default_target(resolved.git_root)
    return GitIntent(
        mode=ns.mode,
        message=ns.message,
        title=ns.title or ns.message,
        requested_branch=ns.branch,
        target=target,
        base=ns.base or f"origin/{target}",
        explicit_base=bool(ns.base),
        files=[f.strip() for f in ns.files.split(",") if f.strip()] if ns.files else [],
        repo=resolved.git_root,
        source=how,
        invoke_cwd=invoke_cwd,
    )


def prepare_branch(intent: GitIntent, ctx: RepoContext, current: str | None, plan: list[str]) -> BranchResult:
    """Phase 2: position the working branch — intent-driven, independent of where HEAD sits
    (see decide_branch for the cut/continue/error policy)."""
    protected = git_state.is_protected_branch(current)
    stale = ctx.branch_pr_inactive()
    action, detail = decide_branch(
        current, intent.requested_branch, protected=protected, stale=stale, base=intent.base,
    )
    if action == "error":
        raise SmartError(
            f"on '{current}' ({detail}) — pass --branch <name> to cut a fresh branch off {intent.base}."
        )
    if action == "cut":
        cut_new_branch(intent.repo, intent.requested_branch, intent.base, plan)
        return BranchResult(branch=intent.requested_branch, cut=True)
    if ctx.branch_pr_in_flight():
        # continuing onto a branch whose PR is still open — the loop's between-rounds state.
        pr = ctx.current_pr()
        label = pr.label if pr else "PR"
        plan.append(f"continuing in-flight {label} on '{current}'")
    return BranchResult(branch=current or "", cut=False)


def stage_and_commit(intent: GitIntent, plan: list[str]) -> StageResult:
    """Phase 3: normalize --files, stage (sensitive blocklist + gitlink guard), commit
    when anything is staged."""
    files = normalize_files(intent.repo, intent.files, intent.invoke_cwd, plan) if intent.files else []
    stage(intent.repo, files, plan)
    warn_mixed_version_bump(intent.repo, plan)
    staged = gitcmd.git(intent.repo, "diff", "--cached", "--name-only").out.strip()
    if not staged:
        plan.append("nothing staged — skipped commit")
        return StageResult(committed=False)
    run(intent.repo, "commit", "-m", intent.message)
    plan.append("committed")
    return StageResult(committed=True)


def publish(intent: GitIntent, branch: BranchResult, staged: StageResult, plan: list[str]) -> None:
    """Phase 4: foreign-commit self-check, then push / MR according to mode."""
    repo, target, current = intent.repo, intent.target, branch.branch

    # Foreign-commit guard: a branch cut fresh off origin/<target> (no --base stacking)
    # must carry ONLY this run's commit(s). More means it was cut off the wrong ref — the
    # classic footgun where a prior gcampr left HEAD on an unrelated feature branch — and the
    # MR would smuggle foreign commits into <target>. Catch at creation, not at merge.
    if branch.cut and not intent.explicit_base:
        ahead = run(repo, "rev-list", "--count", f"origin/{target}..{current}").strip()
        expected = 1 if staged.committed else 0
        if ahead.isdigit() and int(ahead) > expected:
            subjects = run(repo, "log", "--oneline", f"origin/{target}..{current}")
            raise SmartError(
                f"'{current}' has {ahead} commit(s) vs origin/{target} but this run added {expected} — "
                f"it carries foreign commits (cut off a non-{target} base?). Re-cut off origin/{target}:\n{subjects}"
            )

    if intent.mode in ("push", "mr"):
        run(repo, "push", "-u", "origin", current, timeout=60)
        plan.append(f"pushed origin/{current}")

    if intent.mode == "mr":
        rng = run(repo, "log", "--oneline", f"origin/{target}..{current}").strip()
        plan.append(f"PR carries {len(rng.splitlines()) if rng else 0} commit(s) vs origin/{target}")
        pr = reuse_or_create_pr(repo, current, target, intent.title, plan)
        RepoContext.refresh_branch(repo)
        # Don't write pr_number here — keep the `pr` segment single-owner. Trigger one
        # monitor poll so it (the sole writer) populates number + window for the new branch.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from poll_pr_status import poll_once
            poll_once(repo)
        except Exception:
            pass
        plan.append(f"{pr.label}: {pr.web_url}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["commit", "push", "mr"])
    ap.add_argument("--message", "-m", required=True)
    ap.add_argument("--branch", "-b", default=None, help="branch name to cut when on a protected/stale branch")
    ap.add_argument("--target", "-t", default=None)
    ap.add_argument(
        "--base",
        default=None,
        help="ref to cut --branch off (default origin/<target>); pass a feature branch for intentional stacking",
    )
    ap.add_argument("--files", "-f", default=None, help="comma-separated explicit files to stage")
    ap.add_argument("--title", default=None, help="MR title (defaults to commit message)")
    ap.add_argument(
        "--repo", "-r", default=None,
        help="repo to operate on: a path or a workspace subproject name; "
             "default = cwd's repo, falling back to the workspace's last-active repo",
    )
    ns = ap.parse_args(argv)

    try:
        intent = resolve_intent(ns, os.getcwd())
    except SmartError as e:
        print(f"smart_git_ops: {e}", file=sys.stderr)  # no PLAN yet — nothing was attempted
        return 1

    ctx = RepoContext.load(intent.repo) or RepoContext.refresh_all(intent.repo)
    current = git_state.get_current_branch(intent.repo)
    plan: list[str] = [
        f"mode={intent.mode} repo={Path(intent.repo).name} ({intent.source}) "
        f"branch={current} target={intent.target}"
    ]
    try:
        branch = prepare_branch(intent, ctx, current, plan)
        staged = stage_and_commit(intent, plan)
        publish(intent, branch, staged, plan)
        RepoContext.refresh_branch(intent.repo)
    except SmartError as e:
        _banner(plan)
        print(f"\n✗ {e}", file=sys.stderr)
        return 1

    _banner(plan)
    return 0


def _banner(plan: list[str]) -> None:
    print("PLAN:")
    for line in plan:
        print(f"  - {line}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
