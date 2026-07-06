#!/usr/bin/env python3
"""PreToolUse (Edit/Write/MultiEdit/NotebookEdit): 编辑侧策略引擎入口。

把这次文件改动投影成 `FileChange`，跑 FILE_CHANGE 规则（checkout 占有、分支失活、
requirements.txt、层级依赖 lint），deny 则在落盘前拦下。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, rules  # noqa: E402
from lib.context import friction  # noqa: E402
from lib.core import engine  # noqa: E402
from lib.core.context import PolicyContext  # noqa: E402

_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool(*_FILE_TOOLS):
        return None
    change = engine.project(inp)
    ctx = PolicyContext(inp.cwd, anchor_path=inp.file_path, session_id=inp.session_id)
    decision = engine.evaluate(change, ctx, rules.REGISTRY)
    if not decision.blocked:
        return None
    friction.record_deny(decision, tool=inp.tool_name, cwd=inp.cwd,
                         session_id=inp.session_id)  # best-effort; never affects the verdict
    return decision.message()


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
