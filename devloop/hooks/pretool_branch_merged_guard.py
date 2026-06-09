#!/usr/bin/env python3
"""PreToolUse (Edit/Write): deny editing when the current branch's PR/MR is merged/closed.

Catches "PR merged externally; AI still editing the stale branch". Routes through
`lib.context.gate` (LIVE branch + LIVE HEAD, SHA-validated against the cached PR window),
NOT the cached `RepoContext` snapshot — a branch switch via an unobserved channel must not
leave this guard blocking edits on the old branch's merged PR (see gate.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout  # noqa: E402
from lib.context import gate, pr_label  # noqa: E402


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Edit", "Write", "NotebookEdit"):
        return None
    # Resolve from the edited file, not cwd — at an aggregate-workspace root a
    # cwd-based lookup finds no repo and the guard silently never fires.
    git_root = repo_layout.find_git_root(inp.edited_dir())
    if not git_root:
        return None
    gv = gate.evaluate(git_root)
    if not gv.inactive():
        return None
    cur = gv.branch or "?"
    target = gv.target
    pr = gv.active_pr
    pr_str = f"{pr_label(gv.provider, pr.number)} {pr.state}" if pr else "its PR/MR merged/closed"
    return (
        f"⚠️  Branch '{cur}' is no longer active ({pr_str} on origin/{target}).\n"
        "Editing this stale branch wastes work — changes won't reach a fresh MR.\n"
        f"Cut a new branch from latest origin/{target} first:\n"
        f"  /gcampr <new-feature-name> 'your commit msg'\n"
        f"or:  git fetch origin {target} && git checkout -b <name> origin/{target}"
    )


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
