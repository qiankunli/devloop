"""guest session 在别的 session 占有的 checkout 上切分支 → 拦并引导 worktree。

per-checkout owner 锁的主动半边：第一个工作的 session 占有；guest 的 `git switch` /
`git checkout <branch>` 被拦。owner 自己不被拦。`checkout -- <file>`（文件恢复，不动 HEAD）放行。
**有副作用**：free/stale/mine 时 (re)acquire，故 check() 非纯函数。
"""
from __future__ import annotations

from pathlib import Path

from domain import repo_layout
from lib import git_state
from domain.context import session
from hooks.core.domain import Command, Finding, Severity, TargetKind
from hooks.core.protocol import Rule


class CheckoutOwnerGuardRule(Rule):
    name = "checkout-owner"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        sub = target.subcommand
        if sub == "switch":
            return True
        if sub == "checkout":
            return "--" not in target.args  # 排除 `checkout -- <file>` 文件恢复
        return False

    def check(self, target: Command, ctx) -> list[Finding]:
        sid = ctx.session_id
        if not sid:
            return []
        git_root = repo_layout.find_git_root(target.run_dir)
        if not git_root:
            return []
        owner = session.foreign_owner(git_root, sid)
        if owner:
            name = Path(git_root).name
            return [
                Finding(
                    rule=self.name,
                    severity=Severity.DENY,
                    message=(
                        f"⚠️  This checkout is in use by another devloop session "
                        f"(branch '{owner.get('branch') or '?'}', session {str(owner.get('session_id', ''))[:8]}…). "
                        f"Switching branches here would scramble its working tree.\n"
                        f"Work in an isolated git worktree instead — e.g. "
                        f"`/enter {name} --worktree <tag>`."
                    ),
                    locator=" ".join(target.argv),
                )
            ]
        session.acquire(git_root, sid, git_state.get_current_branch(git_root) or "")
        return []
