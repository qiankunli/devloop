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

from lib import git_state, gitcmd, workspace  # noqa: E402
from lib.context import WorkspaceContext  # noqa: E402
from lib.repo_resolve import best_score, looks_like_path  # noqa: E402  (shared fuzzy scoring)

MAX_CANDIDATES = 4


def emit(line: str, code: int) -> int:
    print(line)
    return code


def make_worktree(repo_dir: str, tag: str) -> tuple[str | None, str]:
    """Create/reuse `worktrees/<tag>` (branch `worktree-<tag>`) off origin/<target>.
    Returns (path, message). Idempotent: existing worktree dir is reused."""
    base = Path(repo_dir)
    wt_path = base / "worktrees" / tag
    if wt_path.is_dir():
        return str(wt_path.resolve()), "reused existing worktree"
    target = git_state.get_default_target(repo_dir)
    branch = f"worktree-{tag}"
    r = gitcmd.git(repo_dir, "worktree", "add", "-b", branch,
                   str(Path("worktrees") / tag), f"origin/{target}", timeout=30)
    if not r.ok:
        # branch may already exist → try without -b
        r2 = gitcmd.git(repo_dir, "worktree", "add", str(Path("worktrees") / tag), branch, timeout=30)
        if not r2.ok:
            return None, f"worktree add failed: {r.err or r2.err}"
    return str(wt_path.resolve()), "created worktree"


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
        return None, 1, f"NONE\tworkspace {ws_root} has no subprojects parsed from AGENTS.md"

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
        return emit(f"MATCH\t{wt}", 0)
    return emit(f"MATCH\t{path}", 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
