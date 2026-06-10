#!/usr/bin/env python3
"""PreToolUse (Edit/Write/NotebookEdit): refuse editing a checkout another devloop
session owns — and claim ownership on the first edit.

The passive half of the per-checkout owner lock (see lib/context/session.py). The checkout
guard alone proved insufficient in practice: two concurrent sessions never ran
`git switch` at all — the second one just started editing the same working tree
directly, and nothing stood in its way. So:

- **Acquire follows mutating activity ONLY**: the first session to edit claims here
  (checkout guard / posttool git refresh are the other acquire points). Entering or
  reading a repo deliberately does NOT acquire — the lock protects the checkout's
  mutable surface (working tree / index / branch position); a read-only session
  owning it would just manufacture false conflicts.
- **A guest's edits are denied** with worktree guidance — concurrent writes to one
  working tree mix both sessions' diffs into the same `git status`, and either
  side's commit/lint/test then operates on the other's half-done work. The narrow
  legitimate case (deliberate shared editing) keeps a human-sized escape hatch:
  the user can delete `.devloop/owner.lock`.
- **Gitignored files are exempt**: they never show in `git status` nor get staged,
  so writing them (eval outputs, run logs…) can't mix into the owner's diff.
  Checked only on the contended path to keep the every-edit hot path subprocess-free.

The repo is resolved from the EDITED FILE's path, not cwd (see HookInput.file_path).
Fails open like every guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import git_state, gitcmd, hook_io, repo_layout  # noqa: E402
from lib.context import session  # noqa: E402


def _gitignored(git_root: str, path: str) -> bool:
    """True only when git affirmatively says ignored (rc=0) — a git error (rc=128/-1)
    must NOT widen the exemption, so it falls through to the deny."""
    return gitcmd.git(git_root, "check-ignore", "-q", "--", path).ok


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Edit", "Write", "NotebookEdit"):
        return None
    sid = inp.session_id
    if not sid:
        return None  # can't attribute ownership without a session id — don't gate
    git_root = repo_layout.find_git_root(inp.edited_dir())
    if not git_root:
        return None
    owner = session.foreign_owner(git_root, sid)
    if owner:
        # gitignored file → invisible to the owner's status/diff/commit, no mixing
        # possible; allow without claiming ownership (the checkout stays the owner's)
        if _gitignored(git_root, str(Path(inp.cwd) / inp.file_path)):
            return None
        name = Path(git_root).name
        return (
            f"⚠️  This checkout ('{name}') is in use by another devloop session "
            f"(branch '{owner.get('branch') or '?'}', session {str(owner.get('session_id', ''))[:8]}…). "
            f"Editing it would mix your changes into that session's working tree.\n"
            f"Work in an isolated git worktree instead — e.g. `/enter {name} --worktree <tag>` "
            f"(then edit under the worktree path it prints).\n"
            f"If you intentionally share this checkout, ask the user to remove "
            f"`{git_root}/.devloop/owner.lock` and retry."
        )
    # free / stale / mine → claim it, so the first session to edit becomes the owner
    session.acquire(git_root, sid, git_state.get_current_branch(git_root) or "")
    return None


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
