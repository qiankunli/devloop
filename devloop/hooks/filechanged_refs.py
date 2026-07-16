#!/usr/bin/env python3
"""FileChanged: an AGENTS.md we registered (SessionStart watchPaths) changed on
disk → re-parse References and force a re-inject next prompt.

Native replacement for old hook-driven AGENTS.md mtime polling. We only ever register
AGENTS.md paths to watch, so any FileChanged here means a References source moved
— re-parse + clear the session cadence so UserPromptSubmit re-emits the new refs.
(Refreshes the cwd repo/workspace; precise per-path routing is unnecessary since
only AGENTS.md files are watched.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io
from lib import repo_layout, workspace  # noqa: E402
from lib.context import RepoContext, WorkspaceContext  # noqa: E402


def _changed_path(inp: hook_io.HookInput) -> str | None:
    """Best-effort extraction of the changed file path from the FileChanged payload.
    The exact field name isn't firmly documented, so probe the common shapes."""
    raw = inp.raw
    for k in ("path", "file_path", "changed_path"):
        v = raw.get(k)
        if isinstance(v, str) and v:
            return v
    for k in ("changed_paths", "paths"):
        v = raw.get(k)
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]
    ti = raw.get("tool_input") or {}
    for k in ("path", "file_path"):
        v = ti.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def handle(inp: hook_io.HookInput) -> None:
    # Route to the repo/workspace OF THE CHANGED FILE (so watching all subprojects'
    # AGENTS.md refreshes the right one), falling back to cwd when the path is absent.
    changed = _changed_path(inp)
    base_dir = str(Path(changed).parent) if changed else inp.cwd
    git_root = repo_layout.find_git_root(base_dir)
    if git_root:
        ctx = RepoContext.refresh_all(git_root)   # re-parse AGENTS.md References
        ctx.reset_session_injection()
    ws_root = workspace.find_containing_workspace(base_dir)
    if ws_root:
        ws = WorkspaceContext.refresh(ws_root)
        ws.reset_session_injection()


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
