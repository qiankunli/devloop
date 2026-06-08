#!/usr/bin/env python3
"""Update a merge request — thin wrapper over the GitLab facade.

Usage: update_mr.py <iid> [--title T] [--description D] [--target-branch B] [repo_dir]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib.gitlab import GitLabClient, GitLabError, MergeRequests  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iid")
    ap.add_argument("--title")
    ap.add_argument("--description")
    ap.add_argument("--target-branch", dest="target_branch")
    ap.add_argument("repo_dir", nargs="?", default=".")
    ns = ap.parse_args(argv)

    m = re.search(r"\d+", ns.iid)
    if not m:
        print(f"update_mr: bad iid {ns.iid!r}", file=sys.stderr)
        return 1
    fields = {k: v for k, v in (("title", ns.title), ("description", ns.description),
                                ("target_branch", ns.target_branch)) if v is not None}
    if not fields:
        print("update_mr: nothing to update", file=sys.stderr)
        return 1
    cl = GitLabClient.for_repo(ns.repo_dir)
    if cl is None:
        print("update_mr: no GitLab token (set gitlab.token in ~/.devloop/config.json or $GITLAB_TOKEN) or not a GitLab repo", file=sys.stderr)
        return 0
    try:
        mr = MergeRequests(cl).update(int(m.group()), **fields)
    except GitLabError as e:
        print(f"update_mr: {e}", file=sys.stderr)
        return 1
    print(f"updated MR !{mr['iid']}: {mr['title']} [{mr['state']}] → {mr['web_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
