#!/usr/bin/env python3
"""Update a pull/merge request — thin wrapper over the forge facade.

Usage: update_pr.py <number> [--title T] [--description D] [--target-branch B] [repo_dir]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib.forge import ForgeError, forge_for_repo  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("number")
    ap.add_argument("--title")
    ap.add_argument("--description")
    ap.add_argument("--target-branch", dest="target_branch")
    ap.add_argument("repo_dir", nargs="?", default=".")
    ns = ap.parse_args(argv)

    m = re.search(r"\d+", ns.number)
    if not m:
        print(f"update_pr: bad number {ns.number!r}", file=sys.stderr)
        return 1
    # Neutral field names map to each forge's API inside the adapter (description→body for GitHub).
    fields = {k: v for k, v in (("title", ns.title), ("body", ns.description),
                                ("target_branch", ns.target_branch)) if v is not None}
    if not fields:
        print("update_pr: nothing to update", file=sys.stderr)
        return 1
    forge = forge_for_repo(ns.repo_dir)
    if forge is None:
        print("update_pr: no token or unsupported remote", file=sys.stderr)
        return 0
    try:
        pr = forge.update(int(m.group()), **fields)
    except ForgeError as e:
        print(f"update_pr: {e}", file=sys.stderr)
        return 1
    print(f"updated {pr.label}: {pr.title} [{pr.state}] → {pr.web_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
