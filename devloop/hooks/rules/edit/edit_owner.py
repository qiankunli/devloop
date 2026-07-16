"""guest session 改 owner 占有的 checkout → 拦并引导 worktree。

per-checkout owner 锁的被动半边：第一个做改动的 session 占有该 checkout。
**有副作用**：free/stale/mine 时 acquire（首个编辑即成为 owner），故 check() 非纯函数。
gitignored 文件豁免（不进 owner 的 status/diff，无混入风险）。repo 从被编辑文件解析（ctx.git_root 已锚定）。
"""
from __future__ import annotations

from pathlib import Path

from lib import git_state, gitcmd
from domain.context import session
from hooks.core.domain import FileChange, Finding, Severity, TargetKind
from hooks.core.protocol import Rule


def _gitignored(git_root: str, path: str) -> bool:
    """仅当 git 明确说 ignored(rc=0) 才算；git 出错(rc=128/-1)不放宽豁免。"""
    return gitcmd.git(git_root, "check-ignore", "-q", "--", path).ok


class EditOwnerGuardRule(Rule):
    name = "edit-owner"
    target_kind = TargetKind.FILE_CHANGE

    def check(self, target: FileChange, ctx) -> list[Finding]:
        sid = ctx.session_id
        if not sid:
            return []  # 无 session id → 无法归属占有，不 gate
        git_root = ctx.git_root
        if not git_root:
            return []
        owner = session.foreign_owner(git_root, sid)
        if owner:
            if _gitignored(git_root, ctx.anchor_abspath):
                return []  # gitignored → 不进 owner diff，放行且不抢占
            name = Path(git_root).name
            return [
                Finding(
                    rule=self.name,
                    severity=Severity.DENY,
                    message=(
                        f"⚠️  This checkout ('{name}') is in use by another devloop session "
                        f"(branch '{owner.get('branch') or '?'}', session {str(owner.get('session_id', ''))[:8]}…). "
                        f"Editing it would mix your changes into that session's working tree.\n"
                        f"Work in an isolated git worktree instead. In Claude Code, run "
                        f"`/enter {name} --worktree <tag>`. In Codex, choose a short unique tag and run "
                        f"`python3 \"${{PLUGIN_ROOT}}/scripts/enter.py\" {name} --worktree <tag>`. "
                        f"Read the returned `MATCH\\t<path>`, set subsequent tool calls' `workdir` "
                        f"to that path, then retry this edit there.\n"
                        f"If you intentionally share this checkout, ask the user to remove "
                        f"`{git_root}/.devloop/owner.lock` and retry."
                    ),
                    locator=target.path,
                )
            ]
        # free / stale / mine → 占有（首个编辑成为 owner）
        session.acquire(git_root, sid, git_state.get_current_branch(git_root) or "")
        return []
