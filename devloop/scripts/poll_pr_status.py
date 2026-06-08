#!/usr/bin/env python3
"""PR-sweep monitor — the native replacement for an old hook-driven scheduler.

Declared in monitors/monitors.json; Claude Code runs it as a per-session background
process and surfaces each stdout line as a notification. It periodically polls the repo's
forge (GitHub / GitLab, via the facade) and writes the monitor-owned `pr` segment —
`.devloop/pr.json` (`{branch, provider, pr_number, prs}`, the recent-PR window, cap 5) —
so the existing PreToolUse guards and prompt injection read fresh PR state with zero extra
work. It is the *sole* writer of that file (disjoint from every other writer-role), so no
lock is needed. No daemon, no heartbeat hooks, no scheduler.

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
from lib.forge import ForgeError, build_window, forge_for_repo, pr_label, vocab  # noqa: E402


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


def poll_once(repo: str) -> str | None:
    """One poll: refresh prs + branch.pr_number. Returns a short change summary, or None
    (no token / unsupported remote / nothing). Never raises."""
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

    # Write only the monitor-owned `pr` segment (branch-keyed). No load/merge of the
    # whole context — disjoint from every other writer, so no lock and no lost update.
    prev_seg = base.load_segment(repo, "pr") or {}
    prev = (prev_seg.get("pr_number"),
            tuple((p.get("number"), p.get("state")) for p in (prev_seg.get("prs") or [])))
    git_state.ensure_gitignore_excluded(repo)   # keep /.devloop/ out of git if pr.json is the first file
    base.save_segment(repo, "pr", {
        "branch": branch,
        "provider": forge.provider,
        "pr_number": anchor,
        "prs": [asdict(p) for p in window],
    })
    new = (anchor, tuple((p.number, p.state) for p in window))
    if new == prev:
        return None
    noun = vocab(forge.provider)[0]
    cur = next((p for p in window if p.number == anchor), None)
    if cur:
        return (f"{pr_label(forge.provider, cur.number)} {cur.state} ({cur.source_branch})"
                f" · {len(window)} recent {noun}(s) tracked")
    return f"{len(window)} recent {noun}(s) tracked"


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
    # --quiet: emit nothing to stdout (still polls + writes .devloop/pr.json, so the PR
    # guard / prompt injection stay fresh). This is the DEFAULT in monitors.json — and it
    # has to be, for a non-obvious reason worth recording:
    #
    # poll_once already dedups (emits <=1 line per 90s poll, only on a real PR/MR change),
    # so it's tempting to think on-change is enough and --quiet is redundant. It is NOT.
    # The chat-spam ("Monitor event: ...") is not produced by us: Claude Code's harness
    # re-delivers a long-lived monitor task's notification on ~every turn — measured at
    # ~5x/min, 362 deliveries from ONE underlying event over a 69-min session — regardless
    # of how much we print. The multiplication happens downstream of this script, so
    # on-change dedup cannot reduce the *frequency*; only producing zero stdout can. Hence
    # --quiet is the actual noise control. Do not "simplify" by dropping it on the theory
    # that on-change suffices (verified false). Revisit only if the harness ever stops
    # re-delivering monitor notifications natively.
    quiet = "--quiet" in argv
    args = [a for a in argv if a not in ("--once", "--quiet")]
    target = args[0] if args else "."
    if once:
        for r in repos_to_poll(target):
            msg = poll_once(r)
            if msg and not quiet:
                print(f"devloop: {msg}")
        return 0
    # monitor loop: each tick, poll every repo in scope (all subprojects in Mode A),
    # emit on change, sleep. Never crashes the session.
    while True:
        try:
            for r in repos_to_poll(target):
                msg = poll_once(r)
                if msg and not quiet:
                    print(f"devloop: {msg}", flush=True)
        except Exception:
            pass
        time.sleep(PR_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
