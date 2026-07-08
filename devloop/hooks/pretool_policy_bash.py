#!/usr/bin/env python3
"""PreToolUse (Bash): 命令侧策略引擎入口。

把命令投影成若干 Command + 原始命令串，跑 COMMAND/CHANGE 规则（保护分支、checkout 占有、
git add -A、workspace 根、裸 pytest、pip install、precommit gate），deny 则拦下。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, rules  # noqa: E402
from lib.context.loopstate import friction  # noqa: E402
from lib.core import engine  # noqa: E402
from lib.core.context import PolicyContext  # noqa: E402


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    inp = _with_effective_cwd(inp)
    change = engine.project(inp)
    ctx = PolicyContext(inp.cwd, session_id=inp.session_id)
    decision = engine.evaluate(change, ctx, rules.REGISTRY)
    if not decision.blocked:
        return None
    friction.record_deny(decision, tool=inp.tool_name, cwd=inp.cwd,
                         session_id=inp.session_id)  # best-effort; never affects the verdict
    return decision.message()


def _with_effective_cwd(inp: hook_io.HookInput) -> hook_io.HookInput:
    """Codex may keep hook `cwd` at the session root while a tool call has `workdir`.

    Command rules judge where the command will run, so prefer the tool-level working
    directory when present. Claude-style Bash payloads don't have this field and keep
    using `cwd`.
    """
    wd = (inp.tool_input or {}).get("workdir")
    if not isinstance(wd, str) or not wd.strip():
        return inp
    p = Path(wd).expanduser()
    if not p.is_absolute():
        p = Path(inp.cwd or ".") / p
    return replace(inp, cwd=str(p))


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
