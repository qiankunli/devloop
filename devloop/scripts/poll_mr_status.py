#!/usr/bin/env python3
"""MR-sweep monitor — the native replacement for an old hook-driven scheduler.

Declared in monitors/monitors.json; Claude Code runs it as a per-session background
process and surfaces each stdout line as a notification. It periodically polls GitLab
(via the facade) and writes the monitor-owned `mr` segment — `.devloop/mr.json`
(`{branch, mr_iid, mrs}`, the [anchor-2, latest] window, cap 5) — so the existing
PreToolUse guards and prompt injection read fresh MR state with zero extra work. It is
the *sole* writer of that file (disjoint from every other writer-role), so no lock is
needed. No daemon, no heartbeat hooks, no scheduler.

Branch-MR selection mirrors the read_branch logic: prefer an open MR for the branch; else
the most-recent finished MR whose source SHA is an ancestor of HEAD (so a deleted+rebuilt
branch's stale merged MR doesn't falsely mark the active branch inactive).

Usage:
  poll_mr_status.py <repo_or_project_dir>           # loop forever (monitor mode)
  poll_mr_status.py <repo_or_project_dir> --once     # single poll (testing)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from dataclasses import asdict  # noqa: E402

from lib import git_state, gitcmd, repo_layout, workspace  # noqa: E402
from lib.context import WorkspaceContext, base  # noqa: E402
from lib.context.base import MR_POLL_INTERVAL_SEC  # noqa: E402
from lib.gitlab import GitLabClient, GitLabError, MergeRequests, to_mrrefs  # noqa: E402


def _is_ancestor(repo: str, sha: str | None, head: str) -> bool:
    if not sha or not head:
        return False
    if sha == head:
        return True
    return gitcmd.git(repo, "merge-base", "--is-ancestor", sha, head).rc == 0


def pick_branch_mr(branch_mrs: list[dict], repo: str, head_sha: str) -> dict | None:
    """Choose the MR that represents the current branch (or None).

    Open MR wins (branch reused for new work). Otherwise the most-recent finished
    MR whose source SHA is reachable from HEAD — dead-ref MRs (same branch name,
    unrelated history) are skipped so they don't mark a rebuilt branch inactive.
    """
    opens = [m for m in branch_mrs if m.get("state") == "opened"]
    if opens:
        return opens[0]
    for m in branch_mrs:                       # list() returns created_at desc
        if _is_ancestor(repo, m.get("sha"), head_sha):
            return m
    return None


def poll_once(repo: str) -> str | None:
    """One poll: refresh mrs + branch.mr_iid. Returns a short change summary, or None
    (no token / not GitLab / nothing). Never raises."""
    cl = GitLabClient.for_repo(repo)
    if cl is None:
        return None
    mrs_api = MergeRequests(cl)
    branch = git_state.get_current_branch(repo)
    head = gitcmd.git(repo, "rev-parse", "HEAD").out
    try:
        branch_mr = pick_branch_mr(mrs_api.list(source_branch=branch, state="all"), repo, head) if branch else None
        anchor = int(branch_mr["iid"]) if branch_mr else None
        window = mrs_api.window(anchor)
    except GitLabError:
        return None
    refs = to_mrrefs(window)

    # Write only the monitor-owned `mr` segment (branch-keyed). No load/merge of the
    # whole context — disjoint from every other writer, so no lock and no lost update.
    prev_seg = base.load_segment(repo, "mr") or {}
    prev = (prev_seg.get("mr_iid"),
            tuple((m.get("iid"), m.get("state")) for m in (prev_seg.get("mrs") or [])))
    git_state.ensure_gitignore_excluded(repo)   # keep /.devloop/ out of git if mr.json is the first file
    base.save_segment(repo, "mr", {
        "branch": branch,
        "mr_iid": anchor,
        "mrs": [asdict(m) for m in refs],
    })
    new = (anchor, tuple((m.iid, m.state) for m in refs))
    if new == prev:
        return None
    cur = next((m for m in refs if m.iid == anchor), None)
    if cur:
        return f"MR #{cur.iid} {cur.state} ({cur.source_branch}) · {len(refs)} recent MR(s) tracked"
    return f"{len(refs)} recent MR(s) tracked"


def repos_to_poll(target: str) -> list[str]:
    """Which repos to keep fresh this tick.

    The monitor process can't see the session's cwd (the AI's `cd`s happen in its
    own tool subprocesses), so binding to the startup dir would go stale the moment
    the session moves between subprojects. Resolution:
    - **Mode A** (target under a registered workspace): poll ALL of the workspace's
      subprojects, so whichever one the AI is editing has fresh MR state. Re-read each
      tick to pick up newly added subprojects.
    - **Mode B**: the single repo at/above target.
    """
    ws = workspace.find_containing_workspace(target)
    if ws:
        ctx = WorkspaceContext.load(ws) or WorkspaceContext.refresh(ws)
        repos: list[str] = []
        for s in ctx.subprojects:
            gr = repo_layout.find_git_root(str((Path(ws) / (s.path or s.name)).resolve()))
            if gr and gr not in repos:
                repos.append(gr)
        if repos:
            return repos
    gr = repo_layout.find_git_root(target)
    return [gr] if gr else []


def main(argv: list[str]) -> int:
    once = "--once" in argv
    args = [a for a in argv if a != "--once"]
    target = args[0] if args else "."
    if once:
        for r in repos_to_poll(target):
            msg = poll_once(r)
            if msg:
                print(f"devloop: {msg}")
        return 0
    # monitor loop: each tick, poll every repo in scope (all subprojects in Mode A),
    # emit on change, sleep. Never crashes the session.
    while True:
        try:
            for r in repos_to_poll(target):
                msg = poll_once(r)
                if msg:
                    print(f"devloop: {msg}", flush=True)
        except Exception:
            pass
        time.sleep(MR_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
