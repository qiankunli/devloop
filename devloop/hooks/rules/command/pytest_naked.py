"""仓里有 `make test` 目标时拦裸 `pytest`（裸 pytest 常因缺 PYTHONPATH=. 收集 0 项）。

`Command` 同时携带 env 与 working_dir，避免为拿其中一个事实重新解析原始命令并丢掉另一个。
"""
from __future__ import annotations

import os
from domain import repo_layout
from hooks.core.domain import Command, Finding, Severity, TargetKind
from hooks.core.protocol import Rule


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
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return not target.env and _is_pytest(target.argv)

    def check(self, target: Command, ctx) -> list[Finding]:
        run_dir = target.working_dir.path
        if run_dir is None:
            return []
        git_root = repo_layout.find_git_root(run_dir)
        if not git_root:
            return []
        component = repo_layout.enclosing_component(run_dir, git_root)
        if not component.has_target("test", suffix=True):
            return []
        code_dir = component.path
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    "⚠️  Bare pytest often fails (PYTHONPATH=. missing → collected 0 items).\n"
                    f"Use the Makefile target:  cd {code_dir} && make test\n"
                    f"Single-case debug:  cd {code_dir} && PYTHONPATH=. .venv/bin/python -m pytest <path> -k <case>"
                ),
                locator=" ".join(target.argv),
            )
        ]
