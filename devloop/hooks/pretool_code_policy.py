#!/usr/bin/env python3
"""PreToolUse (Edit/Write/MultiEdit/NotebookEdit): 代码侧策略引擎入口。

把这次文件改动投影成 `FileChange`，跑 FILE_CHANGE 规则（当前=层级依赖），
deny 则在落盘前拦下。命令侧（Bash）由后续迁入的 `pretool_policy_bash` 承接。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io  # noqa: E402
from lib import rules  # noqa: E402
from lib.core import engine  # noqa: E402
from lib.core.context import PolicyContext  # noqa: E402

_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool(*_FILE_TOOLS):
        return None
    change = engine.project(inp)
    ctx = PolicyContext(inp.cwd, anchor_path=inp.file_path)
    decision = engine.evaluate(change, ctx, rules.REGISTRY)
    return decision.message() if decision.blocked else None


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
