"""拦 `git add -A` / `--all` / `.` / `./`，逼显式 staging。"""
from __future__ import annotations

from hooks.core.domain import Command, Finding, Severity, TargetKind
from hooks.core.protocol import Rule

_ADD_ALL_ARGS = {"-A", "--all", ".", "./"}


class AddAllRule(Rule):
    name = "add-all"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand == "add"

    def check(self, target: Command, ctx) -> list[Finding]:
        if not (_ADD_ALL_ARGS & set(target.args)):
            return []
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    "⚠️  Refusing `git add -A` / `git add --all` / `git add .`.\n"
                    "These globs often capture IDE configs (.idea/), env files (.env), or unrelated "
                    "plan / scratch files. Stage files explicitly:\n"
                    "  git add path/to/file1 path/to/file2\n"
                    "Or use the plugin's `smart_gcam.sh` / `stage_and_commit` flow which skips sensitive files."
                ),
                locator=" ".join(target.argv),
            )
        ]
