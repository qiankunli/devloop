#!/usr/bin/env python3
"""Initialize devloop state for a git repo: build the `.devloop/` state segments.

Usage: init_repo.py [path]   (defaults to cwd)
Idempotent — re-running just refreshes. Hooks also auto-init on first cd.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib import repo_layout  # noqa: E402
from lib.context import RepoContext  # noqa: E402


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    git_root = repo_layout.find_git_root(target)
    if not git_root:
        print(f"devloop: not a git repo: {target}", file=sys.stderr)
        return 1
    ctx = RepoContext.refresh_all(git_root)
    print(f"devloop initialized: {git_root}")
    print(f"  code_dir   = {ctx.repo.code_dir}")
    print(f"  language   = {ctx.repo.language}")
    print(f"  branch     = {ctx.branch.local.name} (protected={ctx.branch.local.is_protected()}, target={ctx.branch.target})")
    print(f"  agents_md  = {ctx.agents_md.path} ({len(ctx.agents_md.references)} references)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
