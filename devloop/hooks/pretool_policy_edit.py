#!/usr/bin/env python3
"""PreToolUse (Edit/Write/MultiEdit/NotebookEdit/apply_patch/Codex exec): 变更策略引擎入口。

把文件改动投影成 `FileChange`；Codex 的统一 exec envelope 还会还原内层命令。随后跑匹配
规则（checkout 占有、分支失活、命令守卫等），deny 则在落盘前拦下。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, rules  # noqa: E402
from lib.context.loopstate import friction  # noqa: E402
from lib.core import engine  # noqa: E402
from lib.core.context import PolicyContext  # noqa: E402

def decide(inp: hook_io.HookInput) -> str | None:
    change = engine.project(inp)
    if not change.targets:
        return None
    ctx = PolicyContext(inp.cwd, session_id=inp.session_id)
    decision = engine.evaluate(change, ctx, rules.REGISTRY)
    if not decision.blocked:
        return None
    friction.record_deny(decision, tool=inp.tool_name, cwd=inp.cwd,
                         session_id=inp.session_id)  # best-effort; never affects the verdict
    return decision.message()


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
