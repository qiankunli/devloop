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

from lib import events, git_state, gitcmd, repo_layout, workspace  # noqa: E402
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


def poll_once(repo: str) -> events.Event | None:
    """One poll → a `forge` `Event` describing `repo`'s current PR window, or None when the
    repo has no usable forge / remote. `changed` reflects whether the window moved since the
    last saved segment; `summary` is set only on a change. Never raises. Side-effect-free —
    persisting + notifying are the handlers' job (see `persist` / `notify`)."""
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

    prev_seg = base.load_segment(repo, "pr") or {}
    prev = (prev_seg.get("pr_number"),
            tuple((p.get("number"), p.get("state")) for p in (prev_seg.get("prs") or [])))
    new = (anchor, tuple((p.number, p.state) for p in window))
    changed = new != prev
    payload = {
        "branch": branch,
        "provider": forge.provider,
        "pr_number": anchor,
        "prs": [asdict(p) for p in window],
    }
    return events.Event(
        source="forge", type="pr.update", subject=repo, payload=payload,
        summary=_summary(forge, window, anchor) if changed else "", changed=changed,
    )


def _summary(forge, window: list, anchor) -> str:
    noun = vocab(forge.provider)[0]
    cur = next((p for p in window if p.number == anchor), None)
    if cur:
        return (f"{pr_label(forge.provider, cur.number)} {cur.state} ({cur.source_branch})"
                f" · {len(window)} recent {noun}(s) tracked")
    return f"{len(window)} recent {noun}(s) tracked"


# ── handlers (forge source) ───────────────────────────────────────────────────

def persist(event: events.Event) -> None:
    """Write the monitor-owned `pr` segment (branch-keyed) — the *sole* writer of
    `.devloop/pr.json`, disjoint from every other writer-role, so no lock and no lost
    update. Always writes (keeps the PR guard / injection fresh even on a no-op tick)."""
    repo = event.subject
    git_state.ensure_gitignore_excluded(repo)   # keep /.devloop/ out of git if pr.json is the first file
    base.save_segment(repo, "pr", event.payload)


def notify(event: events.Event) -> None:
    """Surface a one-line change summary to stdout → the harness turns it into a chat
    notification. Fires only on a real change. Registered unless `--quiet` (see `main`)."""
    if event.changed and event.summary:
        print(f"devloop: {event.summary}", flush=True)


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
    # guard / prompt injection stay fresh). DEFAULT in monitors.json. This is a deliberate
    # compromise, not the behavior we'd want — read before "simplifying" it away:
    #
    # What we actually want: a PR/MR changes rarely, so letting the monitor surface ONE
    # "Monitor event" the moment it changes would be fine, even useful. poll_once already
    # does exactly that — it dedups and emits <=1 line per 90s poll, only on a real change.
    #
    # Why we can't ship that: the chat-spam isn't ours. Claude Code's harness re-delivers a
    # long-lived monitor task's notification on ~every turn — measured ~5x/min, 362
    # deliveries from ONE underlying event over a 69-min session — independent of how much
    # we print. So on-change dedup cannot cut the *frequency*; the multiplication is
    # downstream of us. And the harness exposes no knob to keep the one-per-change ping
    # while dropping the repeats — the only lever we have is all-or-nothing stdout.
    #
    # The compromise: pick all-off (--quiet). We give up the (wanted) one-ping-per-change,
    # but state stays fresh in pr.json, so nothing functional is lost — only the chat ping.
    # Tracked upstream at anthropics/claude-code#66219. If the harness ever delivers a
    # monitor line once (no per-turn re-delivery), drop --quiet to restore the intended
    # one-ping-per-change behavior.
    quiet = "--quiet" in argv
    args = [a for a in argv if a not in ("--once", "--quiet")]
    target = args[0] if args else "."
    # event handlers: persist always; notify (the stdout→notification) only when not
    # --quiet, per the rationale above. Each poll builds one Event and fans it out.
    handlers: list[events.Handler] = [persist] if quiet else [persist, notify]
    if once:
        for r in repos_to_poll(target):
            ev = poll_once(r)
            if ev:
                events.dispatch(ev, handlers)
        return 0
    # monitor loop: each tick, poll every repo in scope (all subprojects in Mode A),
    # dispatch on each, sleep. Never crashes the session.
    while True:
        try:
            for r in repos_to_poll(target):
                ev = poll_once(r)
                if ev:
                    events.dispatch(ev, handlers)
        except Exception:
            pass
        time.sleep(PR_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
