#!/usr/bin/env python3
"""SessionEnd: release this session's runtime state (normal-exit path) —
checkout owner locks + the workspace active-repo binding.

Without this, a finished session's lock lingers until its recorded pid dies —
and up to OWNER_TTL_SEC when that pid was a transient shell — so a guest would
keep being refused on a checkout nobody owns anymore. Pid liveness stays as the
crash fallback (see session_lock.release); this hook is the immediate path.

Sweep scope mirrors the monitor's repo set (workspace subprojects in Mode A,
the cwd repo in Mode B) plus each repo's linked worktrees — every checkout
carries its own `.devloop/owner.lock`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout, session_lock, workspace  # noqa: E402
from lib.context import RepoContext, WorkspaceContext, clear_active_repo  # noqa: E402


def _candidate_checkouts(cwd: str) -> list[str]:
    """Every checkout this session might own. Enumeration over-approximates on
    purpose: release() ignores locks held by other sessions, so a too-wide sweep
    is free while a too-narrow one strands a lock."""
    repos: list[str] = []
    ws = workspace.find_containing_workspace(cwd)
    if ws:
        ctx = WorkspaceContext.load(ws)
        for s in (ctx.subprojects if ctx else []):
            gr = repo_layout.find_git_root(str((Path(ws) / (s.path or s.name)).resolve()))
            if gr and gr not in repos:
                repos.append(gr)
    gr = repo_layout.find_git_root(cwd)
    if gr and gr not in repos:
        repos.append(gr)
    # linked worktrees each have their own .devloop/ (and lock); branch.json
    # already knows them — no git calls needed at session teardown.
    for r in list(repos):
        rctx = RepoContext.load(r)
        for wt in (rctx.branch.worktrees if rctx else []):
            if wt.path and wt.path not in repos:
                repos.append(wt.path)
    return repos


def handle(inp: hook_io.HookInput) -> None:
    if not inp.session_id:
        return
    ws = workspace.find_containing_workspace(inp.cwd)
    if ws:
        clear_active_repo(ws, inp.session_id)
    for repo in _candidate_checkouts(inp.cwd):
        session_lock.release(repo, inp.session_id)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
