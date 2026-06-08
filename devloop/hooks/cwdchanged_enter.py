#!/usr/bin/env python3
"""CwdChanged: auto-enter the repo at the new cwd.

The native replacement for an old regex-parse-of-`cd` hook:
the harness hands us the authoritative new working directory, so there's no brittle
command parsing. This does the cheap, safe part of `/enter` automatically — refresh
the repo's state segments and clear its injection stamps so the next UserPromptSubmit
surfaces the entered repo's branch / state / References. `/enter` now only owns what
can't be inferred from a cd: fuzzy name resolution and worktree creation.

Assumes `cwd` in the payload is the NEW directory (the event's whole point).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout, workspace  # noqa: E402
from lib.context import RepoContext, WorkspaceContext, record_active_repo, workspace_for_repo  # noqa: E402


def handle(inp: hook_io.HookInput) -> None:
    new_cwd = inp.cwd
    git_root = repo_layout.find_git_root(new_cwd)
    if git_root:
        ctx = (
            RepoContext.refresh_all(git_root)
            if RepoContext.is_stale_at(git_root)
            else RepoContext.load(git_root) or RepoContext.refresh_all(git_root)
        )
        # Surface the entered repo's state/refs on the next prompt.
        ctx.reset_turn_injection()
        ctx.reset_session_injection()
        # deliberately NO owner acquire: enter 只是选中上下文(阅读/review/查日志都走到
        # 这),不碰 checkout 的可变面——占有由第一笔变更动作建立(edit/checkout guard、
        # posttool git refresh),与 gitignored 豁免同一判据:是否污染 owner 的 diff。
        record_active_repo(git_root)
    # Keep the workspace context warm too (no injection reset — same workspace).
    # Containment misses a symlinked subproject (resolve() escapes the workspace tree),
    # so fall back to the subproject-realpath match; an unregistered workspace root
    # auto-registers on first cd into it.
    ws_root = (workspace.find_containing_workspace(new_cwd)
               or (workspace_for_repo(git_root) if git_root else None)
               or workspace.maybe_register_workspace(new_cwd))
    if ws_root:
        ws = WorkspaceContext.load(ws_root)
        if ws is None or ws.is_stale():
            WorkspaceContext.refresh(ws_root)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
