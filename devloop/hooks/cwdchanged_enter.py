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

from lib import git_state, hook_io, repo_layout, workspace  # noqa: E402
from lib.context import (  # noqa: E402
    RepoContext,
    WorkspaceContext,
    base,
    prstate,
    record_active_repo,
    store,
    workspace_for_repo,
)


def _refresh_remote_view_on_enter(git_root: str) -> None:
    """Enter is the intentional "go look at this repo" moment — the one place a bounded network
    refresh is affordable. Pull the server's trunk tips when the monitor-owned snapshot is
    stale/absent, so 'behind / as-of' and the 'trunk moved' warning reflect reality immediately
    rather than up to one monitor cycle (or a colleague's just-pushed commit) later — the
    sandbox-incident class where the agent read a behind checkout as 'latest'.

    Then ONE opportunistic object fetch, but only when the trunk mirror is actually behind the
    true tip — so a clean enter pays nothing, while an enter onto a stale baseline gets a real
    (not relative-to-a-stale-mirror) behind-count. All bounded + best-effort."""
    seg = store.load_segment(git_root, "remote_branches")
    if seg and not base.is_stale(seg.get("fetched_at"), base.REMOTE_VIEW_STALE_SEC):
        return
    prstate.refresh_remote_branches(git_root)
    fresh = RepoContext.load(git_root)
    if fresh is None:
        return
    base_name = fresh.branch.base_branch()
    tip = fresh.branch.remote_tip(base_name)
    if tip and tip.commit and git_state.rev_parse(git_root, f"origin/{base_name}") not in ("", tip.commit):
        git_state.fetch(git_root, base_name)


def handle(inp: hook_io.HookInput) -> None:
    new_cwd = inp.cwd
    git_root = repo_layout.find_git_root(new_cwd)
    if git_root:
        ctx = (
            RepoContext.refresh_all(git_root)
            if RepoContext.is_stale_at(git_root)
            else RepoContext.load(git_root) or RepoContext.refresh_all(git_root)
        )
        _refresh_remote_view_on_enter(git_root)
        # Surface the entered repo's state/refs on the next prompt.
        ctx.reset_turn_injection()
        ctx.reset_session_injection()
        # deliberately NO owner acquire: enter 只是选中上下文(阅读/review/查日志都走到
        # 这),不碰 checkout 的可变面——占有由第一笔变更动作建立(edit/checkout guard、
        # posttool git refresh),与 gitignored 豁免同一判据:是否污染 owner 的 diff。
        record_active_repo(git_root, inp.session_id)
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
