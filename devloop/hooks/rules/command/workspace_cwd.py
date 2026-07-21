"""在聚合 workspace 根直接跑明确的子项目级命令时拦——会失败或跑错对象。

每条 Command 按自己的 working_dir 判定。用 cd-scope 区分——同 shell `cd <sub>` 进了真仓
（放行），子 shell `(cd <sub>); cmd` 对命令无效（仍拦）。位置未知时不做硬判断。
"""
from __future__ import annotations

from domain import workspace
from domain.context import WorkspaceContext, load_active_repo
from hooks.core.domain import Command, Finding, Severity, TargetKind
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
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return _is_project_local_command(target)

    def check(self, target: Command, ctx) -> list[Finding]:
        run_dir = target.working_dir.path
        if run_dir is None or not workspace.is_workspace_root(run_dir):
            return []
        ws = WorkspaceContext.load(run_dir)
        subs = [s.name.strip("`") for s in (ws.subprojects if ws else [])[:10] if s.name]
        hint = ("\nRegistered subprojects: " + ", ".join(subs)) if subs else ""
        active = load_active_repo(run_dir, ctx.session_id)
        active_hint = f"\nLast-active subproject: {active}" if active else ""
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    f"⚠️  You're at the workspace root '{run_dir.resolve()}', not inside a subproject.\n"
                    "Running a subproject-level command here will fail or misbehave.\n"
                    "Either `cd <subproject>` (or /enter <subproject>) first, or use the devloop scripts, "
                    "which resolve the repo themselves (smart_gcam* accept --repo <name|path>; "
                    "run_fixlint/run_tests take it as the first argument)."
                    f"{hint}{active_hint}"
                ),
                locator=" ".join(target.argv),
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
