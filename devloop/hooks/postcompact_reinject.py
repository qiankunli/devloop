#!/usr/bin/env python3
"""PostCompact: clear injection dedup so state re-injects on the next prompt.

Native replacement for an old TTL-safety-net hack: compaction can silently drop
previously-injected state lines, so right after it we drop both cadences' dedup
stamps — the next UserPromptSubmit re-emits References + turn state. (The TTL
backstop in Cadence still exists as defense-in-depth.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io
from domain import repo_layout, workspace  # noqa: E402
from domain.context import RepoContext, WorkspaceContext  # noqa: E402


def handle(inp: hook_io.HookInput) -> None:
    git_root = repo_layout.find_git_root(inp.cwd)
    if git_root:
        ctx = RepoContext.load(git_root)
        if ctx:
            ctx.clear_injection_dedup()
    ws_root = workspace.find_containing_workspace(inp.cwd)
    if ws_root:
        ws = WorkspaceContext.load(ws_root)
        if ws:
            ws.clear_injection_dedup()


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
