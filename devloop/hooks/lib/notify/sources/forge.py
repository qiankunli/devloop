"""Forge wake source — fires on two INDEPENDENT signals over `.devloop/pr.json`:
  kind="pr_change"     a watched PR/MR's lifecycle state changed (open→merged/closed) — level key;
  kind="merge_blocked" the current MR ENTERED an actionable blocker — edge-detected w/ hysteresis.
Both transports consume this source; the trigger logic (lifecycle key + readiness edge-detection)
lives here, not in a runner. It reads only the monitor's `pr.json` — no second forge poll.
"""
from __future__ import annotations

from pathlib import Path

from lib.context import store
from lib.forge import MergeReadiness, pr_label
from lib.notify.base import Notification

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
    async-noisy and is handled by edge-detection in `step` (merge_block_event), not here.

    Adding a NEW wake signal? Check its shape before touching this key. A MONOTONIC signal (never
    flips back) can join this level-equality key. A signal that can FLICKER — asynchronously
    recomputed, with a transient "checking"/unknown window — must instead be edge-detected with
    hysteresis in `step` (hold through the unknown window; wake only on entering), like
    merge_block_event. Folding a flickering signal into a level key fires two spurious wakes per
    recompute — which is why this key stays lifecycle-only."""
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


class ForgeSource:
    """Watches `.devloop/pr.json` for one repo. carry = (lifecycle_key, prev_seg, last_blocker);
    fires `pr_change` on a lifecycle delta and `merge_blocked` on entering a blocker — two
    independent signals tracked separately (level-equality vs edge+hysteresis)."""

    name = "forge"
    instructions = INSTRUCTIONS

    def seed(self, repo: str):
        seg = store.load_segment(repo, "pr")
        blk, _ = merge_block_event(None, (seg or {}).get("merge_readiness"))  # seed; ignore startup edge
        return (seg_key(seg), seg, blk)

    def step(self, repo: str, carry):
        prev_key, prev_seg, prev_blk = carry
        seg = store.load_segment(repo, "pr")
        key = seg_key(seg)
        notes = []
        if seg and key != prev_key:
            notes.append(Notification(content=summarize(prev_seg, seg, repo), kind="pr_change"))
        cur_blk, wake = merge_block_event(prev_blk, (seg or {}).get("merge_readiness"))
        if seg and wake:
            notes.append(Notification(content=_merge_block_msg(seg, wake, repo), kind="merge_blocked"))
        return (key, seg, cur_blk), notes
