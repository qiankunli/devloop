#!/usr/bin/env python3
"""Forge **producer** for the notify port — the primary Wake path of event-driven resume
(docs/event-driven-resume.md). Opt-in / experimental: NOT auto-loaded by the plugin (needs the
`mcp` package + the channels dev flag); enable per session — see docs/event-driven-resume.md.

Composition (greenfield, channel-first):
  - Perceive : the PR-sweep monitor (`poll_pr_status.py`) polls the forge → `.devloop/pr.json`.
  - Wake+Inform : THIS producer watches those pr.json files and, on a state change, builds a
    `notify.Notification` and hands it to a `Notifier` (here a channel) — an idle session wakes
    WITH the diff inline. Delivery is pluggable via `lib/notify`; this file is forge-only.
  - Execute : the woken turn judges relevance + acts per permission mode (see `INSTRUCTIONS`).

It reuses the monitor's repo set (`repos_to_poll`) and change-key, so it tracks exactly what
the monitor tracks — adding only the notification, no second forge poll.

Usage (spawned by a channel/MCP config):
  forge_channel.py <project_dir>     # watches every workspace subproject's pr.json
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
sys.path.insert(0, str(HERE))  # reuse the monitor's repo resolution

from lib.context import base  # noqa: E402
from lib.forge import MergeReadiness, pr_label  # noqa: E402
from lib.notify.base import Notification  # noqa: E402
from lib.notify.channel import run_channel  # noqa: E402
from poll_pr_status import repos_to_poll  # noqa: E402  (reuse, no second forge poll)

POLL_INTERVAL_SEC = 5  # watch the local pr.json files this often (monitor refreshes ~every 90s)

INSTRUCTIONS = (
    'Events from the forge channel arrive as <channel source="forge" kind="...">. kind="pr_change" '
    'fires when a watched PR/MR changes lifecycle state (opened→merged/closed). kind="merge_blocked" '
    "fires when the current MR ENTERS an actionable blocker (merge conflict / unresolved discussions "
    "/ failing CI) — that one usually needs you to act: resolve the conflict, address the comments. "
    "One-way (no reply): read the event, judge relevance to the work in flight, then act or surface "
    "per your permission mode. If nothing is in flight, surface and stop."
)


def _readiness_signal(readiness: str | None):
    """Classify a pr.json `merge_readiness` for wake EDGE-detection — three outcomes, not a bool:
      - the blocker value (e.g. "conflict") if it's an actionable blocker,
      - "" (clear) for a definite non-blocker (ready / needs_approval / draft),
      - None (HOLD) for the async window (checking/unchecked → UNKNOWN) or absent.
    HOLD is the crux: readiness is computed asynchronously, so a just-pushed MR briefly reads UNKNOWN
    mid-recompute. Treating that as "no new info" (hold the last verdict) is what stops a
    conflict→checking→conflict flicker from firing two wakes — the bug the first cut had when it
    folded readiness into seg_key (a level-identity key can't express this hysteresis)."""
    if not readiness:
        return None
    try:
        m = MergeReadiness(readiness)
    except ValueError:
        return None
    if m.blocks_merge:
        return m.value
    return None if m is MergeReadiness.UNKNOWN else ""


def merge_block_event(prev_blocker, readiness):
    """Edge + hysteresis: given the last KNOWN blocker (or None) and the current pr.json readiness,
    return (new_last_blocker, wake_blocker_or_None). HOLD keeps prev and never wakes; a definite
    non-blocker clears to None; ENTERING or CHANGING a blocker wakes; leaving one doesn't (nothing to
    act on). Pure, so the flicker sequence is unit-tested without driving the async loop."""
    sig = _readiness_signal(readiness)
    if sig is None:                       # HOLD — async window / absent: no new info
        return prev_blocker, None
    cur = sig or None                     # "" → cleared
    wake = cur if (cur and cur != prev_blocker) else None
    return cur, wake


def _merge_block_msg(seg: dict, blocker: str, repo: str) -> str:
    n = seg.get("pr_number")
    label = pr_label(seg.get("provider"), n) if n else "the MR"
    branch = seg.get("branch")
    return (f"forge[{Path(repo).name}]: {label} merge-blocked: {blocker} — needs action"
            + (f" [branch={branch}]" if branch else ""))


def seg_key(seg: dict | None):
    """The LIFECYCLE change-key for a `pr` segment: pr_number + each PR's (number, state). None when
    missing — mirrors `prstate.poll_pr`. Deliberately excludes merge_readiness: lifecycle is a clean
    monotonic signal (open→merged/closed), so level-equality is the right wake trigger; readiness is
    async-noisy and is handled by edge-detection in `produce` (merge_block_event), not here."""
    if not seg:
        return None
    return (seg.get("pr_number"),
            tuple((p.get("number"), p.get("state")) for p in (seg.get("prs") or [])))


def summarize(prev: dict | None, cur: dict, repo: str) -> str:
    """One line naming each PR whose lifecycle state transitioned since the last snapshot of `repo`
    (merge blockers are a separate event — see merge_block_event / _merge_block_msg)."""
    prev_state = {p.get("number"): p.get("state") for p in ((prev or {}).get("prs") or [])}
    moved = []
    for p in (cur.get("prs") or []):
        n, st = p.get("number"), p.get("state")
        if prev_state.get(n) != st:
            title = (p.get("title") or "").strip()
            moved.append(f"PR #{n} {prev_state.get(n) or '∅'}→{st}" + (f" ({title})" if title else ""))
    body = "; ".join(moved) or "PR window changed"
    branch = cur.get("branch")
    return f"forge[{Path(repo).name}]: {body}" + (f" [branch={branch}]" if branch else "")


async def produce(notifier, target: str) -> None:
    """Watch every workspace repo's pr.json and wake on two INDEPENDENT signals:
      - lifecycle: the seg_key changed (open→merged/closed) → kind="pr_change";
      - merge-blocked: the current MR ENTERED an actionable blocker → kind="merge_blocked".
    Tracked separately on purpose — lifecycle is clean/monotonic (level-equality suffices), readiness
    is async-noisy and needs edge-detection with hysteresis (merge_block_event) so the 'checking'
    window can't churn two wakes per recompute."""
    import anyio

    seen: dict[str, tuple] = {}   # repo -> (lifecycle_key, seg, last_blocker)
    for r in repos_to_poll(target):
        seg = base.load_segment(r, "pr")
        blk, _ = merge_block_event(None, (seg or {}).get("merge_readiness"))   # seed; ignore startup edge
        seen[r] = (seg_key(seg), seg, blk)
    while True:
        await anyio.sleep(POLL_INTERVAL_SEC)
        for r in repos_to_poll(target):  # re-resolve each tick to pick up new subprojects
            seg = base.load_segment(r, "pr")
            key = seg_key(seg)
            prev_key, prev_seg, prev_blk = seen.get(r, (None, None, None))
            if seg and key != prev_key:
                await notifier.deliver(Notification(content=summarize(prev_seg, seg, r), kind="pr_change"))
            cur_blk, wake = merge_block_event(prev_blk, (seg or {}).get("merge_readiness"))
            if seg and wake:
                await notifier.deliver(Notification(content=_merge_block_msg(seg, wake, r), kind="merge_blocked"))
            seen[r] = (key, seg, cur_blk)


def main(argv: list[str]) -> int:
    import functools

    import anyio

    target = argv[0] if argv else "."
    anyio.run(run_channel, "forge", INSTRUCTIONS, functools.partial(produce, target=target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
