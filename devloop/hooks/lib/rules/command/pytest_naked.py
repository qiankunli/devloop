"""仓里有 `make test` 目标时拦裸 `pytest`（裸 pytest 常因缺 PYTHONPATH=. 收集 0 项）。

CHANGE 级：判定要 **env-aware**（`PYTHONPATH=. pytest` 带 env 前缀 → 放行），而这个事实与
「这条调用在哪跑」（cd scope）必须出自**同一次**解析——分开取就会拿得到 env 就拿不到 run_dir。
`command_invocations` 两者都给（`inv.env` / `inv.run_dir(base)`），故直接用它。
"""
from __future__ import annotations

import os
from pathlib import Path

from lib import repo_layout
from lib.cmdtree import cmdparse
from lib.core.domain import Change, Finding, Severity, TargetKind
from lib.core.protocol import Rule


def _is_pytest(argv: list[str]) -> bool:
    """argv = 已剥 env 的 tokens：这是不是一条 pytest 调用（`uv run pytest` /
    `python -m pytest` 同算）。「裸不裸」由调用方看 `inv.env` 判，不在这里混着做。"""
    toks = argv
    if not toks:
        return False
    if toks[0] == "uv" and "run" in toks[:2]:
        toks = toks[2:]
    base = os.path.basename(toks[0]) if toks else ""
    if base == "pytest":
        return True
    return bool(base.startswith("python") and toks[1:3] == ["-m", "pytest"])


class PytestNakedRule(Rule):
    name = "pytest-naked"
    target_kind = TargetKind.CHANGE

    def applies(self, change: Change, ctx) -> bool:
        return bool(change.command)

    def check(self, change: Change, ctx) -> list[Finding]:
        git_root = repo_layout.find_git_root(change.cwd)
        if not git_root:
            return []
        base = Path(change.cwd or ".")
        for inv in cmdparse.command_invocations(change.command):
            if inv.env or not _is_pytest(inv.argv):
                continue      # 带 env 前缀（如 PYTHONPATH=.）→ 不算裸，放行
            # 按这条调用**实际运行的目录**归属 code unit：`cd cli && pytest` 要查 cli/ 的 Makefile。
            # 此前这里读的是 `change.cwd`——session 的原始 cwd、cd **之前**的位置，于是多代码目录仓里
            # 不管 cd 到哪都去问默认 unit（server/）：cli 有 make test 拦不住，server 有而 cli 没有则误拦。
            # cd 早已被 parser 解析好，guard 不该回头用未解析的 cwd 重猜。
            unit = repo_layout.enclosing_code_unit(inv.run_dir(base), git_root)
            if not unit.has_target("test", suffix=True):
                continue
            code_dir = unit.path
            return [
                Finding(
                    rule=self.name,
                    severity=Severity.DENY,
                    message=(
                        "⚠️  Bare pytest often fails (PYTHONPATH=. missing → collected 0 items).\n"
                        f"Use the Makefile target:  cd {code_dir} && make test\n"
                        f"Single-case debug:  cd {code_dir} && PYTHONPATH=. .venv/bin/python -m pytest <path> -k <case>"
                    ),
                    locator=change.command,
                )
            ]
        return []
