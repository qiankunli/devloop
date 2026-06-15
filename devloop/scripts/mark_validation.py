#!/usr/bin/env python3
"""Mark lint/test as passed in the `.devloop/validation.json` segment — the single write
surface for validation stamps (run_fixlint.py / run_tests.py and manual /lint /test call it).

Usage: mark_validation.py <lint|test> [--repo R | R]   (R = a path or a workspace
subproject name; default = cwd's repo, falling back to the workspace's last-active repo)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib import cli  # noqa: E402
from lib.context import RepoContext  # noqa: E402


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(prog="mark_validation.py")
    ap.add_argument("kind", choices=["lint", "test"])
    cli.add_repo_arg(ap)
    ns = ap.parse_args(argv)
    resolved, _ = cli.resolve_repo_or_exit(ns, "mark_validation")
    repo = resolved.git_root
    ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)
    if ns.kind == "lint":
        ctx.mark_lint_passed()
    else:
        ctx.mark_test_passed()
    print(f"marked {ns.kind} passed for {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
