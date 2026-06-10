#!/usr/bin/env python3
"""PostToolUse (Edit/Write): count edits since the last lint pass.

Feeds the `validation.edits_since_lint` counter shown in the turn injection and
read by the lint gate. Cheap: load + increment + save only when a context exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout  # noqa: E402
from lib.context import RepoContext, record_active_repo  # noqa: E402


def handle(inp: hook_io.HookInput) -> None:
    if not inp.is_tool("Edit", "Write", "NotebookEdit"):
        return
    # Resolve the repo from the edited file, not the session cwd — in an aggregate
    # workspace the cwd sits at the workspace root while edits land inside subprojects.
    git_root = repo_layout.find_git_root(inp.edited_dir())
    if not git_root:
        return
    ctx = RepoContext.load(git_root)
    if ctx:
        ctx.increment_stale_edits()
    record_active_repo(git_root, inp.session_id)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
