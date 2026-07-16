"""当前分支的 PR/MR 已 merged/closed 时拦编辑（防"PR 已合外部、还在改旧分支"）。

走 gate（LIVE 分支 + LIVE HEAD，SHA 校验），非缓存快照——未观测到的切分支不会误拦旧分支的 merged PR。
repo 从被编辑文件解析（ctx.git_root）。
"""
from __future__ import annotations

from domain.context import gate, pr_label
from hooks.core.domain import FileChange, Finding, Severity, TargetKind
from hooks.core.protocol import Rule


class BranchMergedGuardRule(Rule):
    name = "branch-merged"
    target_kind = TargetKind.FILE_CHANGE

    def check(self, target: FileChange, ctx) -> list[Finding]:
        git_root = ctx.git_root
        if not git_root:
            return []
        gv = gate.evaluate(git_root)
        if not gv.inactive():
            return []
        cur = gv.branch or "?"
        tgt = gv.target
        pr = gv.active_pr
        pr_str = f"{pr_label(gv.provider, pr.number)} {pr.state}" if pr else "its PR/MR merged/closed"
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    f"⚠️  Branch '{cur}' is no longer active ({pr_str} on origin/{tgt}).\n"
                    "Editing this stale branch wastes work — changes won't reach a fresh MR.\n"
                    f"Cut a new branch from latest origin/{tgt} first:\n"
                    f"  /gcampr <new-feature-name> 'your commit msg'\n"
                    f"or:  git fetch origin {tgt} && git checkout -b <name> origin/{tgt}"
                ),
                locator=target.path,
            )
        ]
