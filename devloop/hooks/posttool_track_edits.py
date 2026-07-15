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
        # 归属到被编辑文件所在的 unit。走 `enclosing_code_unit`——与 `select_units` /
        # `_dirty_units` 是同一条路径→unit 投影，所以这里累计的 key 与 gate 查的 key 必然对齐；
        # 各写一份推导正是 key 对不上的经典来源。
        unit = repo_layout.enclosing_code_unit(inp.edited_dir(), git_root)
        ctx.increment_stale_edits(unit.id)
    record_active_repo(git_root, inp.session_id)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
