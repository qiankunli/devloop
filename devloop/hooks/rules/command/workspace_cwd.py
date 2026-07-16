"""在聚合 workspace 根直接跑明确的子项目级命令时拦——会失败或跑错对象。

CHANGE 级：跨命令 + 看 cwd 是否 workspace 根。用 cd-scope 区分——同 shell `cd <sub>` 进了真仓
（放行），子 shell `(cd <sub>); cmd` 对命令无效（仍拦）；靠每个 Command 已解析的 run_dir 判断。
"""
from __future__ import annotations

from pathlib import Path

from lib import workspace
from lib.context import WorkspaceContext, load_active_repo
from hooks.core.domain import Change, Command, Finding, Severity, TargetKind
from hooks.core.protocol import Rule

_GLOBAL_FLAGS = {"-g", "--global"}
_HELP_FLAGS = {"-h", "--help", "-v", "--version", "version", "help"}
_NPM_PROJECT_SUBCOMMANDS = {
    "ci", "dedupe", "fund", "i", "install", "link", "list", "ls", "outdated",
    "pack", "prune", "publish", "remove", "restart", "rm", "run", "start",
    "stop", "test", "uninstall", "update", "version",
}
_PNPM_PROJECT_SUBCOMMANDS = {
    "add", "build", "check", "dev", "i", "install", "lint", "list", "ls",
    "pack", "publish", "remove", "restart", "rm", "run", "start", "test",
    "unlink", "up", "update", "version",
}
_YARN_PROJECT_SUBCOMMANDS = {
    "add", "build", "check", "dev", "install", "link", "lint", "pack",
    "publish", "remove", "run", "start", "test", "unlink", "upgrade",
    "version", "workspace", "workspaces",
}
_UV_PROJECT_SUBCOMMANDS = {"add", "build", "export", "lock", "remove", "run", "sync", "tree", "venv"}
_GO_PROJECT_SUBCOMMANDS = {"build", "fmt", "generate", "get", "list", "mod", "run", "test", "vet", "work"}
_CARGO_PROJECT_SUBCOMMANDS = {
    "add", "bench", "build", "check", "clippy", "doc", "fmt", "metadata",
    "remove", "rm", "run", "test", "tree", "update",
}


class WorkspaceCwdRule(Rule):
    name = "workspace-cwd"
    target_kind = TargetKind.CHANGE

    def applies(self, change: Change, ctx) -> bool:
        return change.tool == "Bash"

    def check(self, change: Change, ctx) -> list[Finding]:
        subproj = [
            t for t in change.targets
            if isinstance(t, Command) and _is_project_local_command(t)
        ]
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


def _is_project_local_command(target: Command) -> bool:
    """Only block high-confidence project-local commands; unknown subcommands pass."""
    base = target.base
    args = target.argv[1:]
    if base == "pytest":
        return True
    if base == "make":
        return not args or not all(a in _HELP_FLAGS for a in args)
    if base == "uv":
        return _subcommand(args) in _UV_PROJECT_SUBCOMMANDS
    if base == "go":
        return _subcommand(args) in _GO_PROJECT_SUBCOMMANDS
    if base == "cargo":
        return _subcommand(args) in _CARGO_PROJECT_SUBCOMMANDS
    if base == "npm":
        if any(a in _GLOBAL_FLAGS for a in args):
            return False
        return _subcommand(args) in _NPM_PROJECT_SUBCOMMANDS
    if base == "pnpm":
        if any(a in _GLOBAL_FLAGS for a in args):
            return False
        return _subcommand(args) in _PNPM_PROJECT_SUBCOMMANDS
    if base == "yarn":
        return _subcommand(args) in _YARN_PROJECT_SUBCOMMANDS
    return False


def _subcommand(args: list[str]) -> str:
    for a in args:
        if not a.startswith("-"):
            return a
    return ""
