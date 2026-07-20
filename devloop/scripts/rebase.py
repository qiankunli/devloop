#!/usr/bin/env python3
"""CLI adapter for devloop's safe, resumable branch-rebase transaction."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS.parent))

from domain import rebase as rebase_flow  # noqa: E402
from lib import cli  # noqa: E402


def _run(ns) -> int:
    verb = ns.verb
    resolved, _ = cli.resolve_repo_or_exit(ns, f"rebase {verb}")
    repo = resolved.git_root
    try:
        if verb == "start":
            plan = rebase_flow.start(repo, ns.target)
        elif verb == "continue":
            plan = rebase_flow.continue_rebase(repo)
        elif verb == "finish":
            plan = rebase_flow.finish(repo)
        elif verb == "abort":
            plan = rebase_flow.abort(repo)
        else:
            plan = rebase_flow.status(repo)
    except rebase_flow.RebaseError as exc:
        print(f"rebase {verb}: {exc}", file=sys.stderr)
        return 1

    print("PLAN:")
    for line in plan:
        print(f"  - {line}")
    return 0


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(
        prog="rebase",
        description="Safely rebase an existing remote branch and publish it with an exact SHA lease.",
    )
    sub = ap.add_subparsers(dest="verb", required=True)

    start = sub.add_parser("start", help="capture the remote lease and begin rebasing onto the target")
    start.add_argument(
        "--target",
        default=None,
        help="target branch (default: the open PR/MR target, then the repo trunk)",
    )
    cli.add_repo_arg(start)

    for verb, help_text in (
        ("continue", "continue after resolving and staging rebase conflicts"),
        ("finish", "publish the completed rebase with the captured force-with-lease"),
        ("abort", "abort an in-progress rebase and discard its captured lease"),
        ("status", "show the current rebase transaction and lease"),
    ):
        parser = sub.add_parser(verb, help=help_text)
        cli.add_repo_arg(parser)

    return _run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
