"""Block unmanaged ``git worktree add`` and route creation through devloop."""
from __future__ import annotations

from pathlib import Path

from lib import git_state, repo_layout
from hooks.core.domain import Command, Finding, Severity, TargetKind
from hooks.core.protocol import Rule


class WorktreeAddRule(Rule):
    name = "worktree-add"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand == "worktree" and bool(target.args) and target.args[0] == "add"

    def check(self, target: Command, ctx) -> list[Finding]:
        git_root = repo_layout.find_git_root(target.run_dir)
        worktrees = git_state.list_worktrees(git_root) if git_root else []
        primary = worktrees[0][0] if worktrees else git_root
        repo = Path(primary).name if primary else "<repo>"
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    "Direct `git worktree add` bypasses devloop's managed worktree lifecycle "
                    "(canonical location, base branch, reuse/pruning, and dependency preparation).\n"
                    f"Use `/enter {repo} --worktree <tag>` in Claude Code, or in Codex run "
                    f"`python3 \"${{PLUGIN_ROOT}}/scripts/enter.py\" {repo} --worktree <tag>`. "
                    "The enter flow delegates creation to `lib/worktree.py`."
                ),
                locator=" ".join(target.argv),
            )
        ]
