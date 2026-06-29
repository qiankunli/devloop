#!/usr/bin/env python3
"""PR + remote-branch sweep monitor — keeps `.devloop/pr.json` and
`.devloop/remote_branches.json` fresh.

Declared in monitors/monitors.json; Claude Code runs it as a per-session background process.
Each tick it sweeps every repo in scope (all workspace subprojects in Mode A) and writes the
two monitor-owned segments via `lib.context.prstate` (the single writer of both):
- `pr.json` — the recent PR/MR window + the current branch's number (SHA-ancestry validated).
- `remote_branches.json` — the server's trunk tips, the read-freshness baseline. We poll these
  because a colleague's push moves trunk under you — an unobservable channel the local refresh
  can never see, so only an active poll learns it.
It is the *sole* writer of those files (disjoint from every other writer-role), so no lock is
needed. No daemon, no heartbeat hooks, no scheduler.

Persist-only by design: it does NOT notify / wake the session. Waking on a PR change is the
notify port's job — the forge `Source` (`lib/notify/sources/forge.py`, watches pr.json) driven by
either transport via `scripts/notify.py` — see docs/event-driven-resume.md.

The poll/selection logic lives in `lib.context.prstate` so gcampr and the gate can trigger the
same authoritative refresh without importing this script.

Usage:
  poll_pr_status.py <repo_or_project_dir>           # loop forever (monitor mode)
  poll_pr_status.py <repo_or_project_dir> --once     # single sweep (testing)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import repo_layout, workspace  # noqa: E402
from lib.context import WorkspaceContext, prstate  # noqa: E402
from lib.context.base import PR_POLL_INTERVAL_SEC  # noqa: E402


def sweep_repo(repo: str) -> None:
    """Refresh both monitor-owned segments for one repo (each best-effort, fail-open)."""
    prstate.refresh_pr(repo)
    prstate.refresh_remote_branches(repo)


def repos_to_poll(target: str) -> list[str]:
    """Which repos to keep fresh this tick.

    The monitor process can't see the session's cwd (the AI's `cd`s happen in its own tool
    subprocesses), so binding to the startup dir would go stale the moment the session moves
    between subprojects. Resolution:
    - **Mode A** (target under a registered workspace): poll ALL of the workspace's
      subprojects, so whichever one the AI is editing has fresh state. Re-read each tick to
      pick up newly added subprojects.
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
            sweep_repo(r)
        return 0
    # monitor loop: each tick, sweep every repo in scope and persist. Never crashes the session.
    while True:
        try:
            for r in repos_to_poll(target):
                sweep_repo(r)
        except Exception:
            pass
        time.sleep(PR_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
