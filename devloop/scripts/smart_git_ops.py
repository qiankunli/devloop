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

from lib import cli, git_state, gitcmd, lifecycle, repo_resolve  # noqa: E402
from lib.context import RepoContext, gate, prstate, record_active_repo  # noqa: E402
from lib.forge import Forge, ForgeError, PullRequest, forge_for_repo, pr_label  # noqa: E402


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
    # The message's body (everything after the first line). It becomes the PR/MR
    # description: title alone is the "cram everything into one line" pressure that
    # produced 150-char MR titles over empty descriptions.
    description: str = ""


@dataclass(frozen=True)
class BranchResult:
    branch: str   # the branch this run commits on, after positioning
    cut: bool     # freshly cut this run → foreign-commit guard applies
    # The branch's in-flight (open) PR when continuing onto it — carried from the gate's
    # SHA-validated view so publish() can sync its description without a second forge poll.
    active_pr: PullRequest | None = None


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
    # Carry uncommitted tracked edits onto the fresh branch. A dirty tree whose files differ
    # between HEAD and base makes `checkout -b` refuse ("would be overwritten"), which used to
    # strand work done *before* the branch was decided — e.g. a version bump run on whichever
    # branch HEAD happened to sit on, then `--branch` to a fresh one. Stash → cut → pop lets that
    # work follow you over, so edit-then-cut works as well as cut-then-edit (order stops mattering).
    st = gitcmd.git(repo, "stash", "push", "-m", f"devloop: cutting {name}")
    stashed = st.ok and "No local changes" not in (st.out + st.err)
    r = gitcmd.git(repo, "checkout", "-b", name, base)
    if not r.ok:
        if stashed:
            gitcmd.git(repo, "stash", "pop")   # restore the working tree on the original branch
        raise SmartError(f"could not cut '{name}' off {base}: {r.err}")
    if stashed:
        pop = gitcmd.git(repo, "stash", "pop")
        if not pop.ok:
            raise SmartError(
                f"cut '{name}' off {base} but reapplying your local changes conflicted: {pop.err}\n"
                "The changes are partially applied with conflict markers and kept in `git stash` — "
                "resolve the conflicts, then `git stash drop`."
            )
    plan.append(f"cut new branch '{name}' off {base}" + (" (carried over local changes)" if stashed else ""))


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


def sync_pr_description(forge: Forge, pr: PullRequest, description: str, plan: list[str]) -> None:
    """Append this run's commit body to an existing PR/MR description (creation passes it
    directly; this covers follow-up commits onto an in-flight PR).

    Append-only with a containment guard: a human-edited description survives, and a
    retried run doesn't duplicate its paragraph. Non-fatal by design — by the time this
    runs the commit/push already landed, so a description miss is cosmetic and becomes a
    PLAN note instead of failing the whole run."""
    if not description:
        return
    label = pr_label(forge.provider, pr.number)
    try:
        existing = forge.description(pr.number)
        if description in existing:
            return
        merged = f"{existing.rstrip()}\n\n{description}" if existing.strip() else description
        forge.update(pr.number, body=merged)
        plan.append(f"appended commit body to {label} description")
    except ForgeError as e:
        plan.append(f"⚠ could not sync {label} description (non-fatal): {e}")


def reuse_or_create_pr(repo: str, source: str, target: str, title: str, description: str, plan: list[str]):
    """Open a PR/MR for `source`→`target`, reusing the branch's open one if present.
    Provider-neutral: the forge is resolved from the repo's origin (GitHub or GitLab).
    `description` becomes the body on create, and is appended on reuse."""
    forge = forge_for_repo(repo)
    if forge is None:
        raise SmartError("branch pushed, but no token / unsupported remote — open the PR/MR manually.")
    try:
        existing = next((p for p in forge.prs_for_branch(source) if p.is_open), None)
        if existing:
            plan.append(f"reused open {pr_label(forge.provider, existing.number)}: {existing.web_url}")
            sync_pr_description(forge, existing, description, plan)
            return existing
        pr = forge.create(source_branch=source, target_branch=target, title=title, body=description)
        plan.append(f"created {pr_label(forge.provider, pr.number)}: {pr.web_url}")
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
        title=ns.title or (ns.message.splitlines()[0] if ns.message else ""),
        description=(ns.message.split("\n", 1)[1].strip() if ns.message and "\n" in ns.message else ""),
        requested_branch=ns.branch,
        target=target,
        base=ns.base or f"origin/{target}",
        explicit_base=bool(ns.base),
        files=[f.strip() for f in ns.files.split(",") if f.strip()] if ns.files else [],
        repo=resolved.git_root,
        source=how,
        invoke_cwd=invoke_cwd,
    )


def refusal_detail(gv: gate.GateView, fallback: str) -> str:
    """Enrich a refuse-to-continue reason with the live-polled PR evidence the gate already holds.

    The push gate runs an authoritative forge poll (`live_refresh`) before deciding, so when it
    refuses a stale branch it KNOWS the PR's number/state/sha — but the bare "current branch's MR
    is merged/closed" line dropped all of it. A caller that just created the MR then distrusts the
    verdict as a stale-context false-positive and re-queries the forge by hand to second-guess it.
    Quoting the evidence inline (e.g. "MR !129 merged, sha 541268f") makes the verdict self-proving:
    nothing to re-verify when it's right, self-evidently wrong when it isn't. The protected-branch
    case has no PR — fall back to the plain reason."""
    pr = gv.active_pr
    if pr is None or not pr.inactive:
        return fallback
    bits = [f"{pr_label(gv.provider, pr.number)} {pr.state}"]
    if pr.sha:
        bits.append(f"sha {pr.sha[:9]}")
    if pr.updated_at:
        bits.append(f"updated {pr.updated_at}")
    detail = ", ".join(bits)
    if pr.web_url:
        detail += f" — {pr.web_url}"
    return detail


def prepare_branch(intent: GitIntent, gv: gate.GateView, plan: list[str]) -> BranchResult:
    """Phase 2: position the working branch — intent-driven, independent of where HEAD sits
    (see decide_branch for the cut/continue/error policy). Decides on gate truth (LIVE branch +
    SHA-validated PR state), never the cached RepoContext."""
    action, detail = decide_branch(
        gv.branch, intent.requested_branch, protected=gv.protected(), stale=gv.inactive(), base=intent.base,
    )
    if action == "error":
        raise SmartError(
            f"on '{gv.branch}' ({refusal_detail(gv, detail)}) — "
            f"pass --branch <name> to cut a fresh branch off {intent.base}."
        )
    if action == "cut":
        cut_new_branch(intent.repo, intent.requested_branch, intent.base, plan)
        # Record where we forked from — git doesn't durably keep it, so devloop captures it at
        # the one moment it's known exactly. Sticky across later refreshes (see repo.py).
        fork = intent.base.split("/", 1)[1] if intent.base.startswith("origin/") else intent.base
        RepoContext.refresh_branch(intent.repo).set_fork_from(fork)
        plan.append(f"recorded fork_from={fork}")
        return BranchResult(branch=intent.requested_branch, cut=True)
    if gv.in_flight():
        # continuing onto a branch whose PR is still open — the loop's between-rounds state.
        pr = gv.active_pr
        label = pr_label(gv.provider, pr.number) if pr else "PR"
        plan.append(f"continuing in-flight {label} on '{gv.branch}'")
        return BranchResult(branch=gv.branch or "", cut=False, active_pr=pr)
    return BranchResult(branch=gv.branch or "", cut=False)


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

    if intent.mode == "push" and staged.committed and intent.description and branch.active_pr:
        # gcamp is the natural way to add commits to an in-flight PR — keep its description
        # in step with the new commit's body, same as the mr-mode reuse path does.
        forge = forge_for_repo(repo)
        if forge is not None:
            sync_pr_description(forge, branch.active_pr, intent.description, plan)

    if intent.mode == "mr":
        rng = run(repo, "log", "--oneline", f"origin/{target}..{current}").strip()
        plan.append(f"PR carries {len(rng.splitlines()) if rng else 0} commit(s) vs origin/{target}")
        reuse_or_create_pr(repo, current, target, intent.title, intent.description, plan)
        RepoContext.refresh_branch(repo)
        # Don't write pr_number here — keep the `pr` segment single-owner. Trigger one
        # authoritative poll so it (the sole writer) populates number + window for the new
        # branch — and this one PERSISTS (the old refresh_pr_state discarded the poll).
        prstate.refresh_pr(repo)


# The failure that bites gcampr callers most: a --message whose quotes/specials broke shell
# parsing, so its tail leaks as stray argv → "unrecognized arguments". Handed to cli.ArgParser
# as the extra hint so that error points at single-quoting / --message-file instead of
# dumping bare usage.
MESSAGE_HINT = (
    "hint: --message probably contained quotes/specials that broke shell parsing. "
    "Single-quote it, or pass --message-file <path> (or -F -, reading stdin) — no "
    "shell escaping needed (mirrors `git commit -F`)."
)


def _resolve_message(ns: argparse.Namespace, ap: argparse.ArgumentParser) -> str:
    """The commit message, from --message-file (a path, or '-' for stdin) or inline --message.
    File/stdin is the shell-escaping-free path for multi-line / quote-heavy messages — the
    industry norm (git's `-F`, gh's `--body-file`). Exactly one source is required."""
    if ns.message_file:
        try:
            raw = sys.stdin.read() if ns.message_file == "-" else Path(ns.message_file).read_text(encoding="utf-8")
        except OSError as e:
            ap.error(f"--message-file unreadable: {e}")
        msg = raw.strip("\n")
        if not msg.strip():
            ap.error("--message-file is empty")
        return msg
    if ns.message:
        return ns.message
    ap.error("a commit message is required: pass --message '<msg>' or --message-file <path>")


def _build_parser() -> cli.ArgParser:
    """The arg schema — extracted so tests exercise the SAME parser main() runs (no drift).
    `--message` (inline) and `--message-file` are alternatives; exactly one is required, enforced
    in `_resolve_message` rather than by argparse, so the error can carry the quoting hint."""
    ap = cli.ArgParser(extra_hints=[MESSAGE_HINT])
    ap.add_argument("mode", choices=["commit", "push", "mr"])
    ap.add_argument("--message", "-m", default=None, help="inline commit message (single-quote it)")
    ap.add_argument(
        "--message-file", "-F", default=None,
        help="read the commit message from a file ('-' = stdin); for multi-line / quote-heavy "
             "messages, write it with the Write tool and pass the path — no shell escaping "
             "(mirrors `git commit -F` / `gh --body-file`)",
    )
    ap.add_argument("--branch", "-b", default=None, help="branch name to cut when on a protected/stale branch")
    ap.add_argument("--target", "-t", default=None)
    ap.add_argument(
        "--base",
        default=None,
        help="ref to cut --branch off (default origin/<target>); pass a feature branch for intentional stacking",
    )
    ap.add_argument("--files", "-f", default=None, help="comma-separated explicit files to stage")
    ap.add_argument("--title", default=None, help="MR title (defaults to the message's first line)")
    cli.add_repo_arg(ap, positional=False)  # --repo/-r only; gcampr takes no positional repo
    return ap


def run_lifecycle_gate(repo: str, phase: str, plan: list[str]) -> None:
    """跑某相位的 lifecycle hook（lint/test 等 inline gate），把结果写进 PLAN。

    配置为空 → 静默 no-op（opt-in，零行为变化）。inline gate 失败 → 抛 SmartError 中止本次
    git 动作。pre_commit 故意排在 staging 之前：lint 的 `make fix` 改的文件要被随后的 stage
    收进同一个 commit。

    signal hook（如 code-review）不挡，返回一个后台下游（`res.to_launch`）；本函数把它写成
    PLAN 的 `ARMED:` 行——dispatch 自己**不能**起「跑完唤醒 session」的后台任务（subprocess
    派生的子进程 harness 不跟踪），故交给 agent 读 PLAN 后用 run_in_background 起（见
    docs/code-review.md）。
    """
    res = lifecycle.dispatch(phase, repo)
    if not res.results:
        return
    plan.append(f"{phase}: " + ", ".join(f"{r.name} {'✓' if r.ok else '✗'}" for r in res.results))
    if not res.proceed:
        detail = "\n".join(f"    [{r.name}] {r.summary}" for r in res.failures)
        step = "commit" if phase == "pre_commit" else "MR"
        raise SmartError(f"{phase} gate failed — aborting before {step}:\n{detail}")
    for spec in res.to_launch:
        plan.append(f"ARMED: {' '.join(spec.argv)}")


def main(argv: list[str]) -> int:
    ap = _build_parser()
    ns = ap.parse_args(argv)
    ns.message = _resolve_message(ns, ap)   # file/stdin or inline; exits with a hint if neither

    try:
        intent = resolve_intent(ns, os.getcwd())
    except SmartError as e:
        print(f"smart_git_ops: {e}", file=sys.stderr)  # no PLAN yet — nothing was attempted
        return 1

    # Gate truth (LIVE branch + SHA-validated PR state). For push/mr — outward, hard to
    # reverse — pass live_refresh so an authoritative forge poll runs first: a PR merged on the
    # server the monitor hasn't caught yet still blocks committing onto the dead branch (the
    # exact lag devloop exists to kill). The cached RepoContext stays for prompt hints only.
    gv = gate.evaluate(intent.repo, live_refresh=(intent.mode in ("push", "mr")))
    plan: list[str] = [
        f"mode={intent.mode} repo={Path(intent.repo).name} ({intent.source}) "
        f"branch={gv.branch} target={intent.target}"
    ]
    try:
        branch = prepare_branch(intent, gv, plan)
        run_lifecycle_gate(intent.repo, "pre_commit", plan)   # before staging: lint's `make fix` lands in this commit
        staged = stage_and_commit(intent, plan)
        if intent.mode == "mr":
            run_lifecycle_gate(intent.repo, "pre_mr", plan)   # before push/MR (default empty; opt-in per repo)
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
