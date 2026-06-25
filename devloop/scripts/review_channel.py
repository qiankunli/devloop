#!/usr/bin/env python3
"""Review **producer** for the notify port — wakes an idle session when a background code-review
lands, WITH the findings inline (docs/event-driven-resume.md). Opt-in / experimental: NOT
auto-loaded by the plugin (needs the `mcp` package + the channels dev flag); enable per session —
see docs/event-driven-resume.md.

Composition (greenfield, channel-first), mirroring forge_channel:
  - Perceive : `run_review.py` (detached by the lifecycle review hook) reviews the MR diff and
    writes `.devloop/review.json` (status + comments + counts).
  - Wake+Inform : THIS producer watches those review.json files and, when a NEW actionable review
    lands, builds a `notify.Notification` carrying the findings and hands it to a `Notifier` (here a
    channel) — an idle session wakes WITH the findings inline, no re-read needed.
  - Execute : the woken turn triages the findings + acts per permission mode (see `INSTRUCTIONS`).

It reuses the monitor's repo set (`repos_to_poll`), so it tracks exactly the workspace subprojects
the monitor tracks — adding only the notification, no second review run.

Why only SOME reviews wake (see `wake_key`): a review with nothing to act on (clean / skipped /
still running) carries no signal worth interrupting an idle session for — token is the first
constraint (docs/event-driven-resume.md). Clean reviews still surface via the next-prompt pull in
`context/repo.py`; only findings / failures / errors justify a push.

Usage (spawned by a channel/MCP config):
  review_channel.py <project_dir>    # watches every workspace subproject's review.json
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
from poll_pr_status import repos_to_poll  # noqa: E402  (reuse workspace repo set)

POLL_INTERVAL_SEC = 5     # watch the local review.json files this often
_MAX_FINDINGS = 30        # cap findings listed inline — full set always in review.json
_BODY_CAP = 300           # per-finding body cap — keep the wake payload bounded

INSTRUCTIONS = (
    'Events from the review channel arrive as <channel source="review" kind="review_done">. One '
    "fires when a background code-review (devloop) finishes with something to act on — findings, "
    "files that failed to review, or a review error; the findings are listed inline. One-way (no "
    "reply): triage them against the work in flight (precision first — fix the real ones, say why "
    "the false positives are false), then act or surface per your permission mode. The full result "
    "(all findings) is in that repo's .devloop/review.json. If nothing relevant is in flight, "
    "surface and stop."
)

# Terminal review statuses that carry NO actionable signal — never wake for these.
_NO_WAKE_STATUS = {None, "running", "skipped"}


def wake_key(seg: dict | None):
    """Identity of a WAKE-WORTHY review, or None when there's nothing to wake for.

    None for: missing / still running / skipped / clean (no findings, no failures, not errored) —
    so a key that goes None→value (or value→different value) is exactly 'a new actionable review
    landed'. `generated_at` is in the key so a RE-RUN on the same sha (same findings) still wakes
    once. Terminal review states don't flicker (run_review writes one 'running' then one terminal
    write), so plain key-inequality is the right trigger here — no hysteresis needed, unlike the
    async-noisy merge-readiness signal in forge_channel."""
    if not seg:
        return None
    status = seg.get("status")
    if status in _NO_WAKE_STATUS:
        return None
    count, failed = seg.get("count", 0), seg.get("failed", 0)
    if not count and not failed and status != "error":
        return None  # clean terminal review — surfaced by the next-prompt pull, not worth a push
    return (seg.get("reviewed_sha"), status, round(seg.get("generated_at") or 0, 1))


def summarize(seg: dict, repo: str) -> str:
    """Format a finished review into the wake payload: a one-line head (counts + sha + range) then
    each finding as `path:start-end (alias) — body`, capped. The woken turn can act straight from
    this; review.json holds the full set."""
    sha = (seg.get("reviewed_sha") or "")[:9]
    rng = seg.get("reviewed_range") or ""
    count, failed = seg.get("count", 0), seg.get("failed", 0)
    bits = []
    if count:
        bits.append(f"{count} finding(s)")
    if failed:
        bits.append(f"{failed} file(s) failed")
    if seg.get("status") == "error":
        bits.append("review errored")
    head = f"review[{Path(repo).name}]: {', '.join(bits) or 'done'} on {sha}" + (f" [{rng}]" if rng else "")
    lines = [head]
    comments = seg.get("comments") or []
    for c in comments[:_MAX_FINDINGS]:
        loc = c.get("path", "?")
        s, e = c.get("start_line", 0), c.get("end_line", 0)
        if s or e:
            loc += f":{s}-{e}"
        alias = (c.get("alias") or "").strip()   # which model in the pool produced it (ocr routing alias)
        tag = f" ({alias})" if alias else ""
        body = (c.get("content") or "").strip().replace("\n", " ")
        lines.append(f"  - {loc}{tag} — {body[:_BODY_CAP]}" if body else f"  - {loc}{tag}")
    extra = count - _MAX_FINDINGS
    if extra > 0:
        lines.append(f"  - … +{extra} more — see .devloop/review.json")
    if seg.get("status") == "error":
        msg = (seg.get("message") or "").strip().replace("\n", " ")
        if msg:
            lines.append(f"  error: {msg[:_BODY_CAP]}")
    return "\n".join(lines)


async def produce(notifier, target: str) -> None:
    """Watch every workspace repo's review.json and wake once per NEW actionable review.

    Seed `seen` from the current review.json so a review that finished BEFORE the session started
    doesn't fire on startup (matches forge_channel's seed-and-ignore-startup-edge)."""
    import anyio

    seen: dict[str, object] = {r: wake_key(base.load_segment(r, "review")) for r in repos_to_poll(target)}
    while True:
        await anyio.sleep(POLL_INTERVAL_SEC)
        for r in repos_to_poll(target):  # re-resolve each tick to pick up new subprojects
            seg = base.load_segment(r, "review")
            key = wake_key(seg)
            if key is not None and key != seen.get(r):
                await notifier.deliver(Notification(content=summarize(seg, r), kind="review_done"))
            seen[r] = key


def main(argv: list[str]) -> int:
    import functools

    import anyio

    target = argv[0] if argv else "."
    anyio.run(run_channel, "review", INSTRUCTIONS, functools.partial(produce, target=target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
