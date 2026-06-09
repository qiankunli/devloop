#!/usr/bin/env python3
"""PR-sweep monitor — keeps `.devloop/pr.json` fresh.

Declared in monitors/monitors.json; Claude Code runs it as a per-session background process.
It periodically polls the repo's forge (GitHub / GitLab, via the facade) and writes the
monitor-owned `pr` segment — `.devloop/pr.json` (`{branch, provider, pr_number, prs}`, the
recent-PR window, cap 5) — so the PreToolUse guards and prompt injection read fresh PR state
with zero extra work. It is the *sole* writer of that file (disjoint from every other
writer-role), so no lock is needed. No daemon, no heartbeat hooks, no scheduler.

Persist-only by design: it does NOT notify / wake the session. Waking on a PR change is the
forge channel's job (`scripts/forge_channel.py`, which watches this same pr.json) — see
docs/event-driven-resume.md.

Branch-PR selection mirrors the read_branch logic: prefer an open PR for the branch; else the
most-recent finished PR whose source SHA is an ancestor of HEAD (so a deleted+rebuilt
branch's stale merged PR doesn't falsely mark the active branch inactive).

Usage:
  poll_pr_status.py <repo_or_project_dir>           # loop forever (monitor mode)
  poll_pr_status.py <repo_or_project_dir> --once     # single poll (testing)
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
from lib.context.base import PR_POLL_INTERVAL_SEC  # noqa: E402
from lib.forge import ForgeError, build_window, forge_for_repo  # noqa: E402


def _is_ancestor(repo: str, sha: str | None, head: str) -> bool:
    if not sha or not head:
        return False
    if sha == head:
        return True
    return gitcmd.git(repo, "merge-base", "--is-ancestor", sha, head).rc == 0


def pick_branch_pr(branch_prs: list, repo: str, head_sha: str):
    """Choose the PR that represents the current branch (or None).

    Open PR wins (branch reused for new work). Otherwise the most-recent finished PR
    whose source SHA is reachable from HEAD — dead-ref PRs (same branch name, unrelated
    history) are skipped so they don't mark a rebuilt branch inactive.
    """
    opens = [p for p in branch_prs if p.is_open]
    if opens:
        return opens[0]
    for p in branch_prs:                       # list() returns created desc
        if _is_ancestor(repo, p.sha, head_sha):
            return p
    return None


def poll_once(repo: str) -> dict | None:
    """One poll → the `pr` segment payload for `repo`'s current PR window, or None when the
    repo has no usable forge / remote. Side-effect-free — writing it is `persist`'s job."""
    forge = forge_for_repo(repo)
    if forge is None:
        return None
    branch = git_state.get_current_branch(repo)
    head = gitcmd.git(repo, "rev-parse", "HEAD").out
    try:
        branch_pr = pick_branch_pr(forge.prs_for_branch(branch), repo, head) if branch else None
        anchor = branch_pr.number if branch_pr else None
        window = build_window(forge, anchor)
    except ForgeError:
        return None
    return {
        "branch": branch,
        "provider": forge.provider,
        "pr_number": anchor,
        "prs": [asdict(p) for p in window],
    }


def persist(repo: str, payload: dict) -> None:
    """Write the monitor-owned `pr` segment — the *sole* writer of `.devloop/pr.json`, disjoint
    from every other writer-role, so no lock and no lost update. Always writes (keeps the PR
    guard / injection fresh even on a no-op tick). The wake side lives in the forge channel."""
    git_state.ensure_gitignore_excluded(repo)   # keep /.devloop/ out of git if pr.json is the first file
    base.save_segment(repo, "pr", payload)


def repos_to_poll(target: str) -> list[str]:
    """Which repos to keep fresh this tick.

    The monitor process can't see the session's cwd (the AI's `cd`s happen in its
    own tool subprocesses), so binding to the startup dir would go stale the moment
    the session moves between subprojects. Resolution:
    - **Mode A** (target under a registered workspace): poll ALL of the workspace's
      subprojects, so whichever one the AI is editing has fresh PR state. Re-read each
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
            payload = poll_once(r)
            if payload:
                persist(r, payload)
        return 0
    # monitor loop: each tick, poll every repo in scope (all subprojects in Mode A) and
    # persist. Never crashes the session.
    while True:
        try:
            for r in repos_to_poll(target):
                payload = poll_once(r)
                if payload:
                    persist(r, payload)
        except Exception:
            pass
        time.sleep(PR_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
