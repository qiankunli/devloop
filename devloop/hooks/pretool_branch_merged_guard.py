#!/usr/bin/env python3
"""PreToolUse (Edit/Write): deny editing when the current branch's MR is merged/closed.

Catches "MR merged externally; AI still editing the stale branch". Reads the
*derived* signal (`branch_mr_inactive` joins branch.mr_iid → mrs), not a stored bool.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout  # noqa: E402
from lib.context import RepoContext  # noqa: E402


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Edit", "Write", "NotebookEdit"):
        return None
    # Resolve from the edited file, not cwd — at an aggregate-workspace root a
    # cwd-based lookup finds no repo and the guard silently never fires.
    git_root = repo_layout.find_git_root(inp.edited_dir())
    if not git_root:
        return None
    ctx = RepoContext.load(git_root)
    if ctx is None or not ctx.branch_mr_inactive():
        return None
    cur = ctx.branch.current or "?"
    target = ctx.branch.target or "release"
    mr = ctx.current_mr()
    mr_str = f"MR #{mr.iid} {mr.state}" if mr else "its MR merged/closed"
    return (
        f"⚠️  Branch '{cur}' is no longer active ({mr_str} on origin/{target}).\n"
        "Editing this stale branch wastes work — changes won't reach a fresh MR.\n"
        f"Cut a new branch from latest origin/{target} first:\n"
        f"  /gcampr <new-feature-name> 'your commit msg'\n"
        f"or:  git fetch origin {target} && git checkout -b <name> origin/{target}"
    )


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
