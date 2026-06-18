"""保护分支上拦 `git commit` / `git push`。

每条 commit/push 按它自己的目标 repo（`-C <dir>` 或 cwd）判定，故从 workspace 根对
`git -C subrepo commit` 也能命中。gate.evaluate 读 LIVE 分支（git rev-parse），不读缓存——
经未观测渠道切到保护分支也不会漏判。
"""
from __future__ import annotations

from pathlib import Path

from lib import repo_layout
from lib.context import gate
from lib.core.domain import Command, Finding, Severity, TargetKind
from lib.core.protocol import Rule


class ProtectBranchRule(Rule):
    name = "protect-branch"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand in ("commit", "push")

    def check(self, target: Command, ctx) -> list[Finding]:
        git_root = repo_layout.find_git_root(target.run_dir)
        if not git_root:
            return []
        gv = gate.evaluate(git_root)
        if not gv.protected():
            return []
        where = f" in repo '{Path(git_root).name}'" if target.dash_c else ""
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    f"⚠️  Refusing `git commit/push` on protected branch '{gv.branch or '?'}'{where}.\n"
                    f"Create a feature branch first: `git checkout -b <name> origin/{gv.target}` "
                    f"(or use /gcampr to do it properly)."
                ),
                locator=" ".join(target.argv),
            )
        ]
