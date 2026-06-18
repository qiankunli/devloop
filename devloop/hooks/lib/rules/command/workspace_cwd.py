"""在聚合 workspace 根直接跑子项目级命令（make/uv/pytest/go/npm…）时拦——会失败或跑错对象。

CHANGE 级：跨命令 + 看 cwd 是否 workspace 根。用 cd-scope 区分——同 shell `cd <sub>` 进了真仓
（放行），子 shell `(cd <sub>); cmd` 对命令无效（仍拦）；靠每个 Command 已解析的 run_dir 判断。
"""
from __future__ import annotations

from pathlib import Path

from lib import workspace
from lib.context import WorkspaceContext, load_active_repo
from lib.core.domain import Change, Command, Finding, Severity, TargetKind
from lib.core.protocol import Rule

_SUBPROJECT_CMDS = {"make", "uv", "pytest", "go", "npm", "pnpm", "yarn", "cargo"}


class WorkspaceCwdRule(Rule):
    name = "workspace-cwd"
    target_kind = TargetKind.CHANGE

    def applies(self, change: Change, ctx) -> bool:
        return change.tool == "Bash"

    def check(self, change: Change, ctx) -> list[Finding]:
        subproj = [t for t in change.targets if isinstance(t, Command) and t.base in _SUBPROJECT_CMDS]
        if not subproj:
            return []
        cwd_resolved = Path(change.cwd).resolve()
        if cwd_resolved not in {Path(w).resolve() for w in workspace.load_workspaces()}:
            return []
        # 仅当子项目命令真的在 workspace 根执行才拦（run_dir 已含 cd-scope）
        if not any(t.run_dir.resolve() == cwd_resolved for t in subproj):
            return []
        ws = WorkspaceContext.load(cwd_resolved)
        subs = [s.name.strip("`") for s in (ws.subprojects if ws else [])[:10] if s.name]
        hint = ("\nRegistered subprojects: " + ", ".join(subs)) if subs else ""
        active = load_active_repo(cwd_resolved, ctx.session_id)
        active_hint = f"\nLast-active subproject: {active}" if active else ""
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    f"⚠️  You're at the workspace root '{cwd_resolved}', not inside a subproject.\n"
                    "Running a subproject-level command here will fail or misbehave.\n"
                    "Either `cd <subproject>` (or /enter <subproject>) first, or use the devloop scripts, "
                    "which resolve the repo themselves (smart_gcam* accept --repo <name|path>; "
                    "run_fixlint/run_tests take it as the first argument)."
                    f"{hint}{active_hint}"
                ),
                locator=change.command,
            )
        ]
