#!/usr/bin/env python3
"""PostToolUse (Bash/Codex exec): refresh state without Claude's CwdChanged event.

Claude Code gives devloop a native CwdChanged event, so the normal Bash post-tool hook
only has to react to git mutations. Codex currently exposes PostToolUse but not
CwdChanged/FileChanged/SessionEnd, so this hook is the Codex fallback: keep the current
cwd and command-scoped repos warm, bind the active repo, then reuse the normal git
mutation refresh for branch/owner updates.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import posttool_git_refresh  # noqa: E402
from lib import hook_io, repo_layout, workspace  # noqa: E402
from lib.context import RepoContext, WorkspaceContext, record_active_repo, workspace_for_repo  # noqa: E402
from lib.core import engine  # noqa: E402
from lib.core.domain import Command, FileChange  # noqa: E402


def _candidate_roots_for_input(inp: hook_io.HookInput) -> list[str]:
    """Resolve repos touched by either a native Bash call or a unified exec envelope."""
    roots: list[str] = []

    def add(path: str | Path) -> None:
        root = repo_layout.find_git_root(path)
        if root and root not in roots:
            roots.append(root)

    add(inp.cwd)
    for target in engine.project(inp).targets:
        if isinstance(target, Command):
            add(target.run_dir)
            if target.base == "cd" and len(target.argv) >= 2 and not target.argv[1].startswith("-"):
                path = Path(os.path.expanduser(os.path.expandvars(target.argv[1])))
                add(path if path.is_absolute() else target.run_dir / path)
        elif isinstance(target, FileChange):
            path = Path(target.path)
            file_path = path if path.is_absolute() else Path(inp.cwd) / path
            add(file_path.parent)
    return roots


def _warm_repo(git_root: str, session_id: str) -> None:
    ctx = (
        RepoContext.refresh_all(git_root)
        if RepoContext.is_stale_at(git_root)
        else RepoContext.load(git_root) or RepoContext.refresh_all(git_root)
    )
    record_active_repo(git_root, session_id)
    # Codex does not document a session id environment variable for model-run shell
    # commands. Keep an anonymous binding as a best-effort fallback for scripts invoked
    # from an aggregate workspace root; session-scoped bindings remain the precise path
    # when the CLI provides an env id.
    if not (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_SESSION_ID")):
        record_active_repo(git_root, "")
    ctx.emit_turn_if_changed()  # cheap read path; keeps corrupt branch segments fail-open


def _warm_workspace(cwd: str, roots: list[str]) -> None:
    ws_root = workspace.find_containing_workspace(cwd)
    if not ws_root:
        for root in roots:
            ws_root = workspace_for_repo(root)
            if ws_root:
                break
    if not ws_root:
        ws_root = workspace.maybe_register_workspace(cwd)
    if ws_root:
        ws = WorkspaceContext.load(ws_root)
        if ws is None or ws.is_stale():
            WorkspaceContext.refresh(ws_root)


def handle(inp: hook_io.HookInput) -> None:
    if not inp.is_tool("Bash", "exec"):
        return
    roots = _candidate_roots_for_input(inp)
    for root in roots:
        _warm_repo(root, inp.session_id)
    _warm_workspace(inp.cwd, roots)
    posttool_git_refresh.handle(inp)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
