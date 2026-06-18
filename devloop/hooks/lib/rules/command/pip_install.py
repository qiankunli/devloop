"""uv-managed 仓里拦 `pip install`（放行 `pip install -e .`）。"""
from __future__ import annotations

import os
from pathlib import Path

from lib import repo_layout
from lib.context import RepoContext
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
        git_root = repo_layout.find_git_root(ctx.cwd)
        if not git_root:
            return []
        rc = RepoContext.load(git_root)
        code_dir = Path(rc.repo.code_dir if rc else git_root)
        if not ((code_dir / "pyproject.toml").exists() and (code_dir / "uv.lock").exists()):
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
