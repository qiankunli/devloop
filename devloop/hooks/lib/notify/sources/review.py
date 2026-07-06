"""Review wake source — fires when a background `run_review` lands a terminal, ACTIONABLE review
(findings / file failures / error) on `.devloop/review.json`, carrying the findings inline. Clean /
running / skipped don't fire — nothing to act on, left to the next-prompt pull in `context/repo.py`.

`wake_key` is the single definition of "wake-worthy"; both transports consume this source, so the
channel push and the waiter exit agree byte-for-byte on when a review wakes the session.
"""
from __future__ import annotations

from pathlib import Path

from lib import git_state
from lib.context import base
from lib.notify.base import Notification

_MAX_FINDINGS = 30        # cap findings listed inline — full set always in review.json
_BODY_CAP = 300           # per-finding body cap — keep the wake payload bounded

# Terminal review statuses that carry NO actionable signal — never wake for these.
_NO_WAKE_STATUS = {None, "running", "skipped"}

INSTRUCTIONS = (
    'Events from the review channel arrive as <channel source="review" kind="review_done">. One '
    "fires when a background code-review (devloop) finishes with something to act on — findings, "
    "files that failed to review, or a review error; the findings are listed inline. One-way (no "
    "reply): triage them against the work in flight (precision first — fix the real ones, say why "
    "the false positives are false), then act or surface per your permission mode. The full result "
    "(all findings) is in that repo's .devloop/review.json. If nothing relevant is in flight, "
    "surface and stop."
)


def wake_key(seg: dict | None):
    """Identity of a WAKE-WORTHY review, or None when there's nothing to wake for.

    None for: missing / still running / skipped / clean (no findings, no failures, not errored) —
    so a key that goes None→value (or value→different value) is exactly 'a new actionable review
    landed'. `generated_at` is in the key so a RE-RUN on the same sha (same findings) still wakes
    once. Terminal review states don't flicker (run_review writes one 'running' then one terminal
    write), so plain key-inequality is the right trigger here — no hysteresis needed, unlike the
    async-noisy merge-readiness signal in the forge source."""
    if not seg:
        return None
    status = seg.get("status")
    if status in _NO_WAKE_STATUS:
        return None
    count, failed = seg.get("count", 0), seg.get("failed", 0)
    if not count and not failed and status != "error":
        return None  # clean terminal review — surfaced by the next-prompt pull, not worth a wake
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


class ReviewSource:
    """Watches the CURRENT branch's `branches/<b>/review.json` for one repo (review is
    branch-domain state). The branch is re-resolved on every seed/step, so a checkout switch
    while a waiter is armed just repoints the watch — no stale-branch review can fire it.
    carry = the last `wake_key`; fires `review_done` when an actionable terminal review with
    a NEW key lands."""

    name = "review"
    instructions = INSTRUCTIONS

    def _seg(self, repo: str):
        return base.load_segment(repo, base.branch_segment(git_state.get_current_branch(repo), "review"))

    def seed(self, repo: str):
        return wake_key(self._seg(repo))

    def step(self, repo: str, carry):
        seg = self._seg(repo)
        key = wake_key(seg)
        notes = []
        if key is not None and key != carry:
            notes.append(Notification(content=summarize(seg, repo), kind="review_done"))
        return key, notes
