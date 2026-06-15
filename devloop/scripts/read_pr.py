#!/usr/bin/env python3
"""View a pull/merge request — thin wrapper over the forge facade.

Usage: read_pr.py <number | PR/MR-url> [--repo R | R]   (R = a path or a workspace
subproject name; default = cwd's repo, falling back to the workspace's last-active repo)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib import cli  # noqa: E402
from lib.forge import ForgeError, MergeReadiness, forge_for_repo, parse_pr_number, pr_label  # noqa: E402

_READINESS_LABEL = {
    MergeReadiness.READY: "✓ ready",
    MergeReadiness.CONFLICT: "✗ conflict",
    MergeReadiness.DISCUSSIONS_UNRESOLVED: "✗ unresolved discussions",
    MergeReadiness.CI_BLOCKED: "✗ CI blocked",
    MergeReadiness.NEEDS_APPROVAL: "✗ needs approval",
    MergeReadiness.DRAFT: "✗ draft",
    MergeReadiness.UNKNOWN: "? unknown (still checking?)",
}


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(prog="read_pr.py")
    ap.add_argument("number", metavar="number|url", help="PR/MR number or URL")
    cli.add_repo_arg(ap)
    ns = ap.parse_args(argv)
    number = parse_pr_number(ns.number)
    if number is None:
        print(f"read_pr: cannot parse PR/MR number from {ns.number!r}", file=sys.stderr)
        return 1
    resolved, _ = cli.resolve_repo_or_exit(ns, "read_pr")
    forge = forge_for_repo(resolved.git_root)
    if forge is None:
        print("read_pr: no token or unsupported remote", file=sys.stderr)
        return 0
    try:
        pr = forge.get(number)
        comments = forge.comments(number)
        readiness = forge.merge_readiness(number)
    except ForgeError as e:
        print(f"read_pr: {e}", file=sys.stderr)
        return 1
    print(f"{pr_label(forge.provider, pr.number)}: {pr.title}  [{pr.state}]")
    print(f"  {pr.source_branch} → {pr.target_branch}")
    print(f"  merge: {_READINESS_LABEL.get(readiness, readiness.value)}")
    print(f"  {pr.web_url}")
    if comments:
        print(f"  comments ({len(comments)}):")
        for c in comments[:20]:
            body = (c.body or "").strip().replace("\n", " ")
            print(f"    - {c.author}: {body[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
