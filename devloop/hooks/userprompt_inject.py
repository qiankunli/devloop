#!/usr/bin/env python3
"""UserPromptSubmit: inject workspace + repo context into the prompt.

Two cadences (plan §6): session (References/Subprojects — usually emitted once at
SessionStart, re-emitted here only if changed, e.g. after FileChanged) and turn
(branch / dirty / validation / recent-MR digest — volatile, the value source).
Order: session blocks first, volatile turn block last.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout, workspace  # noqa: E402
from lib.context import RepoContext, WorkspaceContext, load_active_repo  # noqa: E402


def produce(inp: hook_io.HookInput) -> str | None:
    parts: list[str] = []

    ws_root = workspace.find_containing_workspace(inp.cwd)
    if ws_root:
        ws = WorkspaceContext.load(ws_root)
        if ws:
            s = ws.emit_session_if_changed()
            if s:
                parts.append(s)
                ws.mark_session_emitted(s)

    git_root = repo_layout.find_git_root(inp.cwd)
    if not git_root and ws_root:
        # The session cwd routinely sits at the aggregate-workspace ROOT (scripts are
        # cwd-independent), where cwd itself is not a git repo. Fall back to the workspace's
        # last-active repo so the turn context — branch topology, remote freshness, the
        # IN-FLIGHT/INACTIVE hints — still reaches the prompt in this most common usage (Codex P1).
        git_root = load_active_repo(ws_root)
    if git_root:
        ctx = RepoContext.load(git_root) or RepoContext.refresh_all(git_root)
        s = ctx.emit_session_if_changed()
        if s:
            parts.append(s)
            ctx.mark_session_emitted(s)
        t = ctx.emit_turn_if_changed()
        if t:
            parts.append(t)
            ctx.mark_turn_emitted(t)

    return "\n\n".join(parts) if parts else None


if __name__ == "__main__":
    raise SystemExit(hook_io.inject(produce, event="UserPromptSubmit"))
