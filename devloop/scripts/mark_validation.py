#!/usr/bin/env python3
"""Mark lint/test as passed in the `.devloop/validation.json` segment — the single write
surface for validation stamps (run_fixlint.py / run_tests.py and manual /lint /test call it).

Usage: mark_validation.py <lint|test> [repo_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib import repo_layout  # noqa: E402
from lib.context import RepoContext  # noqa: E402


def main(argv: list[str]) -> int:
    kind = argv[0] if argv else "lint"
    if kind not in ("lint", "test"):
        print("usage: mark_validation.py <lint|test> [repo_dir]", file=sys.stderr)
        return 1
    repo = repo_layout.find_git_root(argv[1] if len(argv) > 1 else ".")
    if not repo:
        print("mark_validation: not a git repo", file=sys.stderr)
        return 1
    ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)
    if kind == "lint":
        ctx.mark_lint_passed()
    else:
        ctx.mark_test_passed()
    print(f"marked {kind} passed for {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
