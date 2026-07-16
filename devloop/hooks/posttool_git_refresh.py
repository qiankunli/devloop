#!/usr/bin/env python3
"""PostToolUse (Bash/Codex exec): after a git state-changing command, refresh the branch
segment (`.devloop/branch.json`).

cd is NOT handled here — that's the native `CwdChanged` hook's job (older hook setups handled
cd in PostToolUse by regex; devloop uses the authoritative event). This hook only
reacts to commands that change git state (commit/push/checkout/...).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io  # noqa: E402
from domain import repo_layout  # noqa: E402
from lib import git_state  # noqa: E402
from hooks.cmdtree import cmdparse  # noqa: E402
from domain.context import RepoContext, record_active_repo, session  # noqa: E402
from hooks.core import engine  # noqa: E402
from hooks.core.domain import Command  # noqa: E402

_STATE_SUBCOMMANDS = {"commit", "push", "checkout", "switch", "reset", "merge", "rebase", "pull", "fetch"}
# fetch only updates remote-tracking refs — it never touches the working tree or the
# checked-out branch, so a session that merely browses another repo for reference
# (fetch + log + read) must NOT claim that checkout's ownership. It still refreshes
# branch.json (ahead/behind counts depend on the remote refs it just moved).
_OWNERSHIP_SUBCOMMANDS = _STATE_SUBCOMMANDS - {"fetch"}


def affected_roots(command: str, cwd: str, subcommands: set[str] = _STATE_SUBCOMMANDS) -> set[str]:
    """Git roots whose state this command changed.

    Parsed via `cmdparse.git_invocations` (not a regex) so `git -C repo commit` and
    quoted-text false positives are handled. Each invocation is judged in its own
    effective dir — `run_dir` layers `-C` over the position-aware cd-prefix
    over `cwd`, so `cd repo && git push` resolves to repo while a cd AFTER the git
    call can't steal its attribution.
    """
    roots: set[str] = set()
    for inv in cmdparse.git_invocations(command):
        if inv.subcommand not in subcommands:
            continue
        root = repo_layout.find_git_root(inv.run_dir(cwd))
        if root:
            roots.add(root)
    return roots


def _affected_input_roots(inp: hook_io.HookInput, subcommands: set[str]) -> set[str]:
    roots: set[str] = set()
    for target in engine.project(inp).targets:
        if not isinstance(target, Command) or target.subcommand not in subcommands:
            continue
        root = repo_layout.find_git_root(target.run_dir)
        if root:
            roots.add(root)
    return roots


def handle(inp: hook_io.HookInput) -> None:
    if not inp.is_tool("Bash", "exec"):
        return
    owning_roots = _affected_input_roots(inp, _OWNERSHIP_SUBCOMMANDS)
    for git_root in _affected_input_roots(inp, _STATE_SUBCOMMANDS):
        RepoContext.refresh_branch(git_root)
        record_active_repo(git_root, inp.session_id)
        # ownership follows activity: a session doing git work in a checkout claims it
        # (no-op if a foreign session already owns it — the guards handle that side)
        if inp.session_id and git_root in owning_roots:
            session.acquire(git_root, inp.session_id, git_state.get_current_branch(git_root) or "")


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
