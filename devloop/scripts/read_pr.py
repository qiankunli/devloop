#!/usr/bin/env python3
"""View a pull/merge request — thin wrapper over the forge facade.

Usage: read_pr.py <number | PR/MR-url> [repo_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib.forge import ForgeError, forge_for_repo, parse_pr_number, pr_label  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: read_pr.py <number|url> [repo_dir]", file=sys.stderr)
        return 1
    number = parse_pr_number(argv[0])
    if number is None:
        print(f"read_pr: cannot parse PR/MR number from {argv[0]!r}", file=sys.stderr)
        return 1
    repo = argv[1] if len(argv) > 1 else "."
    forge = forge_for_repo(repo)
    if forge is None:
        print("read_pr: no token or unsupported remote", file=sys.stderr)
        return 0
    try:
        pr = forge.get(number)
        comments = forge.comments(number)
    except ForgeError as e:
        print(f"read_pr: {e}", file=sys.stderr)
        return 1
    print(f"{pr_label(forge.provider, pr.number)}: {pr.title}  [{pr.state}]")
    print(f"  {pr.source_branch} → {pr.target_branch}")
    print(f"  {pr.web_url}")
    if comments:
        print(f"  comments ({len(comments)}):")
        for c in comments[:20]:
            body = (c.body or "").strip().replace("\n", " ")
            print(f"    - {c.author}: {body[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
