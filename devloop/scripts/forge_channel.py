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
from lib.notify.base import Notification  # noqa: E402
from lib.notify.channel import run_channel  # noqa: E402
from poll_pr_status import repos_to_poll  # noqa: E402  (reuse, no second forge poll)

POLL_INTERVAL_SEC = 5  # watch the local pr.json files this often (monitor refreshes ~every 90s)

INSTRUCTIONS = (
    'Events from the forge channel arrive as <channel source="forge" kind="pr_change">. They '
    "fire when a watched PR/MR changes state. One-way (no reply): read the event, judge whether "
    "it is relevant to the work in flight in this session, then continue the next step or just "
    "surface it — per your permission mode. If nothing is in flight, surface and stop."
)


def seg_key(seg: dict | None):
    """The monitor's change-key for a `pr` segment: pr_number + each PR's (number, state).
    None when missing — mirrors `prstate.poll_pr` so we react to the same changes."""
    if not seg:
        return None
    return (seg.get("pr_number"),
            tuple((p.get("number"), p.get("state")) for p in (seg.get("prs") or [])))


def summarize(prev: dict | None, cur: dict, repo: str) -> str:
    """One line naming each PR whose state transitioned since the last snapshot of `repo`."""
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
    """Watch every workspace repo's pr.json; deliver a Notification on each PR-window change."""
    import anyio

    seen: dict[str, tuple] = {}
    for r in repos_to_poll(target):
        seg = base.load_segment(r, "pr")
        seen[r] = (seg_key(seg), seg)
    while True:
        await anyio.sleep(POLL_INTERVAL_SEC)
        for r in repos_to_poll(target):  # re-resolve each tick to pick up new subprojects
            seg = base.load_segment(r, "pr")
            key = seg_key(seg)
            prev_key, prev_seg = seen.get(r, (None, None))
            if seg and key != prev_key:
                await notifier.deliver(
                    Notification(content=summarize(prev_seg, seg, r), kind="pr_change")
                )
            seen[r] = (key, seg)


def main(argv: list[str]) -> int:
    import functools

    import anyio

    target = argv[0] if argv else "."
    anyio.run(run_channel, "forge", INSTRUCTIONS, functools.partial(produce, target=target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
