"""仓里有 `make test` 目标时拦裸 `pytest`（裸 pytest 常因缺 PYTHONPATH=. 收集 0 项）。

CHANGE 级：要按 env-aware 的 segment 判断（`PYTHONPATH=. pytest` 带 env 前缀 → 放行），
而投影出的 Command 已剥 env，故直接读原始命令串重解析。
"""
from __future__ import annotations

import os

from lib import repo_layout
from lib.cmdtree import cmdparse
from lib.core.domain import Change, Finding, Severity, TargetKind
from lib.core.protocol import Rule


def _is_naked_pytest(seg: list[str]) -> bool:
    """seg = 一个 segment 的 tokens（env 未剥）。带 env 前缀则不算裸。"""
    if not seg:
        return False
    if "=" in seg[0] and not seg[0].startswith("-"):
        return False  # 有 env 前缀（如 PYTHONPATH=.）→ 放行
    toks = seg
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
        if not any(_is_naked_pytest(seg) for seg in cmdparse.segments(change.command)):
            return []
        git_root = repo_layout.find_git_root(change.cwd)
        if not git_root:
            return []
        # 按命令的实际 cwd 归属到 code unit，而非拿 repo 单值 code_dir——多代码目录仓里
        # 在 cli/ 下跑 pytest，该查 cli/ 的 Makefile，不是默认的 server/。
        unit = repo_layout.enclosing_code_unit(change.cwd, git_root)
        code_dir = unit.path
        if not unit.has_target("test", suffix=True):
            return []
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
