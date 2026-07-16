"""uv-managed 仓里拦 `pip install`（放行 `pip install -e .`）。"""
from __future__ import annotations

import os
from pathlib import Path

from lib import ecosystem, repo_layout
from lib.core.domain import Command, Finding, Severity, TargetKind
from lib.core.protocol import Rule


def _pip_install_args(toks: list[str]) -> list[str] | None:
    """是 pip install 调用则返回 'install' 之后的参数，否则 None。"""
    base = os.path.basename(toks[0]) if toks else ""
    if base in ("pip", "pip3") and "install" in toks[1:]:
        return toks[toks.index("install") + 1 :]
    if base.startswith("python") and toks[1:3] == ["-m", "pip"] and "install" in toks[3:]:
        return toks[toks.index("install") + 1 :]
    return None


class PipInstallRule(Rule):
    name = "pip-install"
    target_kind = TargetKind.COMMAND

    def check(self, target: Command, ctx) -> list[Finding]:
        args = _pip_install_args(target.argv)
        if args is None:
            return []
        if "-e" in args and "." in args:  # 本地 dev 安装，放行
            return []
        git_root = repo_layout.find_git_root(target.run_dir)
        if not git_root:
            return []
        # 按这条命令**实际运行的目录**（`run_dir`，parser 已把 cd / -C 解析完）归属 code unit：
        # uv-managed 判定看你实际所在 unit 的 pyproject+uv.lock，多代码目录仓里不同 unit 的包管理
        # 方式可能不同。**不读 `ctx.cwd`**——那是 session 的原始 cwd、cd 之前的位置，
        # `cd cli && pip install x` 会去问仓根有没有 uv.lock，于是 cli 是 uv 仓也拦不住。
        code_dir = Path(repo_layout.enclosing_code_unit(target.run_dir, git_root).path)
        eco = ecosystem.detect(code_dir)
        if not isinstance(eco, ecosystem.PythonEcosystem) or not eco.is_uv_managed(code_dir):
            return []
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    "⚠️  This repo is uv-managed (pyproject.toml + uv.lock).\n"
                    "Don't use `pip install` directly. Instead:\n"
                    "  uv add <package>    # add a dependency\n"
                    "  uv sync             # install / update from pyproject.toml\n"
                    "`pip install -e .` is allowed for local dev installs."
                ),
                locator=" ".join(target.argv),
            )
        ]
