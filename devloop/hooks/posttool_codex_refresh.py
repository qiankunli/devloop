#!/usr/bin/env python3
"""PostToolUse (Bash) for Codex: refresh state without Claude's CwdChanged event.

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
from lib.cmdtree import cmdparse  # noqa: E402
from lib.context import RepoContext, WorkspaceContext, record_active_repo, workspace_for_repo  # noqa: E402


def _cd_target(inv: cmdparse.Invocation, base: str) -> Path | None:
    if not inv.argv or os.path.basename(inv.argv[0]) != "cd" or len(inv.argv) < 2:
        return None
    target = inv.argv[1]
    if target.startswith("-"):
        return None
    p = Path(os.path.expanduser(os.path.expandvars(target)))
    return p if p.is_absolute() else inv.run_dir(base) / p


def _candidate_roots(command: str, cwd: str) -> list[str]:
    roots: list[str] = []

    def add(path: str | Path) -> None:
        root = repo_layout.find_git_root(path)
        if root and root not in roots:
            roots.append(root)

    add(cwd)
    for inv in cmdparse.command_invocations(command):
        add(inv.run_dir(cwd))
        cd_target = _cd_target(inv, cwd)
        if cd_target is not None:
            add(cd_target)
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
    if not inp.is_tool("Bash"):
        return
    roots = _candidate_roots(inp.command, inp.cwd)
    for root in roots:
        _warm_repo(root, inp.session_id)
    _warm_workspace(inp.cwd, roots)
    posttool_git_refresh.handle(inp)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
