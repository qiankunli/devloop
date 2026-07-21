#!/usr/bin/env python3
"""PreToolUse (Bash): 命令侧策略引擎入口。

把命令投影成若干 Command + 原始命令串，跑 COMMAND/CHANGE 规则（保护分支、checkout 占有、
git add -A、workspace 根、裸 pytest、pip install、precommit gate），deny 则拦下。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io, rules  # noqa: E402
from hooks import friction  # noqa: E402
from hooks.core import engine  # noqa: E402
from hooks.core.context import PolicyContext  # noqa: E402


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    change = engine.project(inp)
    ctx = PolicyContext(inp.cwd, session_id=inp.session_id)
    decision = engine.evaluate(change, ctx, rules.REGISTRY)
    if not decision.blocked:
        return None
    friction.record_deny(decision, tool=inp.tool_name, cwd=inp.cwd,
                         session_id=inp.session_id)  # best-effort; never affects the verdict
    return decision.message()

if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
