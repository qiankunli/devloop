#!/usr/bin/env python3
"""PreToolUse (Bash): refuse a branch switch when another devloop session owns
this checkout — switching would scramble its working tree. Use a worktree.

This is the active half of the per-checkout owner lock (see lib/session_lock):
the first session to work in a checkout owns it; a guest session that tries to
`git switch` / `git checkout <branch>` here is denied and pointed at a worktree.
The owner itself is never blocked. Fails open (any error → allow).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import git_state, hook_io, repo_layout, session_lock  # noqa: E402
from lib.cmdtree import cmdparse  # noqa: E402

_SWITCHERS = {"switch", "checkout"}


def _is_branch_switch(inv: dict) -> bool:
    """True for HEAD-moving forms; False for file restores (`checkout -- <file>`)."""
    sub = inv.subcommand
    if sub == "switch":
        return True
    if sub == "checkout":
        return "--" not in inv.args
    return False


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    sid = inp.session_id
    if not sid:
        return None  # CLI without session id → can't attribute ownership, don't gate
    for inv in cmdparse.git_invocations(inp.command):
        if inv.subcommand not in _SWITCHERS or not _is_branch_switch(inv):
            continue
        git_root = repo_layout.find_git_root(inv.run_dir(inp.cwd))
        if not git_root:
            continue
        owner = session_lock.foreign_owner(git_root, sid)
        if owner:
            name = Path(git_root).name
            return (
                f"⚠️  This checkout is in use by another devloop session "
                f"(branch '{owner.get('branch') or '?'}', session {str(owner.get('session_id', ''))[:8]}…). "
                f"Switching branches here would scramble its working tree.\n"
                f"Work in an isolated git worktree instead — e.g. "
                f"`/enter {name} --worktree <tag>` or "
                f"`git worktree add ../{name}-<tag> <branch>`."
            )
        # free / stale / mine → (re)claim so I stay the owner while active here
        session_lock.acquire(git_root, sid, git_state.get_current_branch(git_root) or "")
    return None


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
