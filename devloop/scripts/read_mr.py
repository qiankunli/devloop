#!/usr/bin/env python3
"""View a merge request — thin wrapper over the GitLab facade.

Usage: read_mr.py <iid | MR-url> [repo_dir]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib.gitlab import GitLabClient, GitLabError, MergeRequests  # noqa: E402


def parse_iid(arg: str) -> int | None:
    m = re.search(r"/merge_requests/(\d+)", arg) or re.fullmatch(r"!?(\d+)", arg)
    return int(m.group(1)) if m else None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: read_mr.py <iid|url> [repo_dir]", file=sys.stderr)
        return 1
    iid = parse_iid(argv[0])
    if iid is None:
        print(f"read_mr: cannot parse MR iid from {argv[0]!r}", file=sys.stderr)
        return 1
    repo = argv[1] if len(argv) > 1 else "."
    cl = GitLabClient.for_repo(repo)
    if cl is None:
        print("read_mr: no GitLab token (set gitlab.token in ~/.config/devloop/config.json or $GITLAB_TOKEN) or not a GitLab repo", file=sys.stderr)
        return 0
    mrs = MergeRequests(cl)
    try:
        mr = mrs.get(iid)
        discussions = mrs.discussions(iid)
    except GitLabError as e:
        print(f"read_mr: {e}", file=sys.stderr)
        return 1
    print(f"MR !{mr['iid']}: {mr['title']}  [{mr['state']}]")
    print(f"  {mr['source_branch']} → {mr['target_branch']}")
    print(f"  {mr['web_url']}")
    notes = [n for d in discussions for n in d.get("notes", []) if not n.get("system")]
    if notes:
        print(f"  comments ({len(notes)}):")
        for n in notes[:20]:
            body = (n.get("body") or "").strip().replace("\n", " ")
            print(f"    - {n.get('author', {}).get('username', '?')}: {body[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
