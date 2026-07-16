#!/usr/bin/env python3
"""Resolve a subproject name (fuzzy) or path to an absolute path; optionally make a worktree.

Used by `/enter`. Context loading on cd is handled by the CwdChanged hook, so this
script only does what a cd can't infer: name→path resolution and worktree creation.

Output protocol (first line):
  MATCH<TAB><absolute_path>
  CANDIDATES<TAB><name1>\\t<path1>\\t<name2>\\t<path2>...
  NONE<TAB><reason>
Exit codes: 0 single match; 2 multiple candidates; 1 no match / error.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "hooks"))

from lib import config, ecosystem, git_state, gitcmd, repo_layout, workspace  # noqa: E402
from lib.context import WorkspaceContext, session  # noqa: E402
from lib.repo_resolve import best_score, looks_like_path  # noqa: E402  (shared fuzzy scoring)

MAX_CANDIDATES = 4


def emit(line: str, code: int) -> int:
    print(line)
    return code


def prepare_worktree(path: str) -> list[str]:
    """把新建/复用 worktree 里的每个 code unit 带到可验证状态；返回环境告警。

    monorepo 的 lockfile 通常只在根 unit：根的一次 install 会准备整个 workspace，子 package
    没 lockfile 会自然 no-op。这里与 lifecycle gate 共用 `ecosystem.ensure_ready`，提前准备是
    降低首次验证延迟，gate 再查一次才是 correctness 兜底。
    """
    warnings = []
    for unit in repo_layout.discover_code_units(path):
        if problem := ecosystem.ensure_ready(unit.path):
            warnings.append(f"unit {unit.id}: {problem}")
    return warnings


def make_worktree(repo_dir: str, tag: str) -> tuple[str | None, str]:
    """Create/reuse a worktree at `.worktrees/<tag>` (branch `worktree-<tag>`) off origin/<target>.
    Worktrees live INSIDE the repo, never as siblings of it. New ones go under `.worktrees/`;
    reuse also accepts the legacy `worktrees/` layout so older checkouts keep resolving.
    Returns (path, message). Idempotent: an existing worktree dir is reused. Each call also
    prunes the repo's oldest surplus worktrees (see `_prune_old_worktrees`)."""
    base = Path(repo_dir)
    rel = Path(".worktrees") / tag
    # reuse: prefer .worktrees/, fall back to the legacy worktrees/ dir
    for legacy in (rel, Path("worktrees") / tag):
        if (base / legacy).is_dir():
            path = str((base / legacy).resolve())
            _prune_old_worktrees(repo_dir, keep_path=path)
            warnings = prepare_worktree(path)
            msg = "reused existing worktree"
            if warnings:
                msg += "; environment warning: " + " | ".join(warnings)
            return path, msg
    target = git_state.local_default_target(repo_dir)
    branch = f"worktree-{tag}"
    r = gitcmd.git(repo_dir, "worktree", "add", "-b", branch,
                   str(rel), f"origin/{target}", timeout=30)
    if not r.ok:
        # branch may already exist → try without -b
        r2 = gitcmd.git(repo_dir, "worktree", "add", str(rel), branch, timeout=30)
        if not r2.ok:
            return None, f"worktree add failed: {r.err or r2.err}"
    path = str((base / rel).resolve())
    _prune_old_worktrees(repo_dir, keep_path=path)
    warnings = prepare_worktree(path)
    msg = "created worktree"
    if warnings:
        msg += "; environment warning: " + " | ".join(warnings)
    return path, msg


def _worktree_activity(path: str) -> float:
    """'Recency' of a worktree = the later of its working-dir mtime and its git *index* mtime.
    The index is rewritten by add / commit / checkout / switch — real work in THIS worktree —
    and is per-worktree, so it ranks each independently. HEAD commit time is deliberately NOT
    used: worktrees branched off the same trunk share a baseline commit, which would flatten
    the ranking into a tie. 0.0 when nothing is stat-able → sorts as oldest."""
    times = []
    try:
        times.append(os.stat(path).st_mtime)
    except OSError:
        pass
    r = gitcmd.git(path, "rev-parse", "--git-path", "index")
    if r.ok and r.out:
        try:
            times.append(os.stat(Path(path) / r.out).st_mtime)
        except OSError:
            pass
    return max(times) if times else 0.0


def _managed_worktrees(repo_dir: str) -> list[str]:
    """Linked worktrees devloop manages: those directly under `<main>/.worktrees/` (or the
    legacy `<main>/worktrees/`). Excludes the primary checkout and any external/sibling
    worktree a user made by hand — those are never touched by pruning."""
    wts = git_state.list_worktrees(repo_dir)
    if not wts:
        return []
    main = Path(wts[0][0]).resolve()          # first entry is always the primary checkout
    homes = {main / ".worktrees", main / "worktrees"}
    return [str(Path(p).resolve()) for p, _sha, _branch in wts[1:] if Path(p).resolve().parent in homes]


def _prune_old_worktrees(repo_dir: str, keep_path: str | None = None) -> None:
    """Keep the `keep_recent` most-recently-active managed worktrees of `repo_dir`; remove the
    rest. Config-driven (`config.worktree(repo_dir)['keep_recent']`) so a repo's `.devloop/
    config.json` overrides the global default. Semantics: N>0 keep the N newest, 0 keep none
    (remove every surplus), N<0 disable pruning entirely. Non-destructive by design: a non-force
    `git worktree remove` refuses a dirty worktree, the just-made `keep_path` is never a target
    (so 0 still spares the worktree you just entered), and a worktree held by another live
    session is skipped. Branches are kept — a removed worktree's `worktree-<tag>` survives and
    re-enter rebuilds the checkout. Best-effort: any git failure leaves that worktree in place."""
    keep = config.worktree(repo_dir).get("keep_recent", 5)
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 5
    if keep < 0:
        return
    managed = _managed_worktrees(repo_dir)
    if len(managed) <= keep:
        return
    keepp = str(Path(keep_path).resolve()) if keep_path else None
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    doomed = sorted(managed, key=_worktree_activity, reverse=True)[keep:]
    pruned = False
    for path in doomed:
        if path == keepp:
            continue
        if sid and session.foreign_owner(path, sid):     # another live session is working on it
            continue
        if gitcmd.git(repo_dir, "worktree", "remove", path, timeout=30).ok:
            pruned = True
    if pruned:
        gitcmd.git(repo_dir, "worktree", "prune", timeout=15)


def parse_args(argv: list[str]) -> tuple[str | None, str | None]:
    """Return (query, worktree_tag). --worktree <tag> is extracted."""
    query, tag, i = None, None, 1
    args = argv[1:]
    out: list[str] = []
    while args:
        a = args.pop(0)
        if a == "--worktree":
            tag = args.pop(0) if args else None
        else:
            out.append(a)
    if out:
        query = " ".join(out).strip()
    return query, tag


def resolve_base(query: str) -> tuple[str | None, int, str]:
    """Resolve query → (path, exit_code, candidates_line). path set iff single match."""
    if looks_like_path(query):
        p = Path(os.path.expanduser(os.path.expandvars(query))).resolve()
        if not p.is_dir():
            return None, 1, f"NONE\tpath does not exist: {p}"
        return str(p), 0, ""

    # auto-register an unregistered workspace root (registry may be empty — it only
    # survives at the user level since v0.0.8, and manual init rarely happens)
    ws_root = workspace.find_containing_workspace(Path.cwd()) or workspace.maybe_register_workspace(Path.cwd())
    if not ws_root:
        return None, 1, (f"NONE\tcwd not under any registered workspace; pass a path "
                         f"or run init_workspace first (query={query!r})")
    ctx = WorkspaceContext.load(ws_root)
    if ctx is None or not ctx.subprojects:
        ctx = WorkspaceContext.refresh(ws_root)
    if not ctx.subprojects:
        return None, 1, (f"NONE\tworkspace {ws_root} has no subprojects "
                         f"(no git-repo subdir/symlink found, and none in AGENTS.md)")

    scored: list[tuple[int, str, str]] = []
    for s in ctx.subprojects:
        if not s.name:
            continue
        sc = best_score(query, [s.name, *s.aliases])
        if sc is None:
            continue
        abs_path = (Path(ws_root) / (s.path or s.name)).resolve()
        if abs_path.is_dir():
            scored.append((sc, s.name, str(abs_path)))
    if not scored:
        names = ", ".join(s.name for s in ctx.subprojects if s.name)
        return None, 1, f"NONE\tno subproject matches {query!r}; known: {names}"
    scored.sort(key=lambda t: (t[0], t[1]))
    exact = [x for x in scored if x[0] == 0]
    if len(exact) == 1 or len(scored) == 1:
        return scored[0][2], 0, ""
    parts: list[str] = []
    for _, name, path in scored[:MAX_CANDIDATES]:
        parts += [name, path]
    return None, 2, "CANDIDATES\t" + "\t".join(parts)


def main(argv: list[str]) -> int:
    query, tag = parse_args(argv)
    if not query:
        return emit("NONE\tno argument given", 1)
    path, code, line = resolve_base(query)
    if path is None:
        return emit(line, code)
    if tag:
        wt, msg = make_worktree(path, tag)
        if wt is None:
            return emit(f"NONE\t{msg}", 1)
        print(f"MATCH\t{wt}")
        print(f"INFO\t{msg}")
        return 0
    return emit(f"MATCH\t{path}", 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
