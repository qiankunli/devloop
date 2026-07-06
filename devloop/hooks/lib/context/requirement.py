"""Requirement scope — the loop-state unit of accumulation: ONE requirement's delivery, from
its first branch cut to PR/MR merge, spanning 1..N branches (stacked PRs, post-merge
follow-ups). See docs/loop-state.md (this is slice 3: the layout + index).

Two artifacts, two writer disciplines:
- `.devloop/requirements.json` — the **index segment** `{branch: requirement_id}` + per-req
  status. A SEGMENT (whole-file overwrite); its sole writer-role is gcampr (via this module).
- `.devloop/requirements/<requirement_id>/session.jsonl` — the requirement's **session
  ledger** (append-only lifecycle spine: session_start / branch_cut / pr_created / …). Named
  after the requirement's FIRST branch — an ID (stable), not a description; later branches
  attach to it without renaming.

`requirement_id == first branch name`, so the id is known at open time with nothing to invent.
Friction/resolution events stay in their own flat ledgers keyed by branch/sha and JOIN to a
requirement via this index at mine time — the hot deny path never reads the index.
"""
from __future__ import annotations

import json

from . import base

_INDEX = "requirements"   # segment name → .devloop/requirements.json + session dir root


def _index(root) -> dict:
    idx = base.load_segment(root, _INDEX) or {}
    idx.setdefault("branches", {})       # branch → requirement_id
    idx.setdefault("requirements", {})   # requirement_id → {status, ts}
    return idx


def resolve(root, branch: str) -> str | None:
    """The requirement id a branch belongs to (its requirement's first-branch name), or None
    if the branch was never attached (e.g. created outside gcampr)."""
    return _index(root)["branches"].get(branch)


def record_event(root, requirement_id: str, event: dict) -> None:
    """Append one event to `requirements/<id>/session.jsonl` (nested name → append_jsonl builds
    the path). Stamps a FULL-precision `ts` — not rounded: the close backstop does `now - ts`
    staleness math, and a rounded-up ts could exceed `now` and flip the age negative. Caller
    supplies `kind` + payload."""
    base.append_jsonl(root, f"{_INDEX}/{requirement_id}/session", {"ts": base.now(), **event})


def open_requirement(root, branch: str, *, fork_from: str | None = None,
                     fork_sha: str | None = None) -> str:
    """Start a new requirement whose id = `branch`. Indexes the branch and emits `session_start`.
    Idempotent: re-opening the same branch is a no-op (no duplicate session_start). Returns the id."""
    idx = _index(root)
    if idx["branches"].get(branch) == branch:
        return branch
    idx["branches"][branch] = branch
    idx["requirements"][branch] = {"status": "open", "ts": round(base.now(), 1)}
    base.save_segment(root, _INDEX, idx)
    record_event(root, branch, {"kind": "session_start", "requirement": branch, "branch": branch,
                                "fork_from": fork_from, "fork_sha": fork_sha})
    return branch


def attach_branch(root, requirement_id: str, branch: str, *, fork_sha: str | None = None) -> None:
    """A subsequent branch joins requirement `requirement_id`. Indexes it and emits
    `branch_cut{continues:true}`. No-op if already attached to that requirement."""
    idx = _index(root)
    if idx["branches"].get(branch) == requirement_id:
        return
    idx["branches"][branch] = requirement_id
    idx["requirements"].setdefault(requirement_id, {"status": "open", "ts": round(base.now(), 1)})
    base.save_segment(root, _INDEX, idx)
    record_event(root, requirement_id, {"kind": "branch_cut", "branch": branch,
                                        "continues": True, "fork_sha": fork_sha})


def note(root, branch: str, event: dict) -> None:
    """Append `event` to the requirement `branch` belongs to. Lazily opens a requirement
    (id = branch) if the branch isn't indexed yet — so an event (e.g. pr_created) on a branch
    created outside gcampr still lands somewhere sensible."""
    req = resolve(root, branch) or open_requirement(root, branch)
    record_event(root, req, event)


# ── close half: monitor-side scope closing ───────────────────────────────────
def session_events(root, requirement_id: str) -> list[dict]:
    """Parse a requirement's session.jsonl into events. Fail-open ([]) on missing/corrupt —
    a close reconcile must never raise inside the monitor sweep."""
    p = base.state_dir(root) / _INDEX / requirement_id / "session.jsonl"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return []
    return out


def reconcile_closures(root, *, stale_after_sec: float = base.REQUIREMENT_STALE_SEC) -> None:
    """Close finished requirements from the monitor side — best-effort, idempotent,
    LEVEL-triggered (reconciles to the desired state, so it's safe to run every tick, on
    restart, or after a missed tick — never an edge diff that a caller could double-fire).

    Reads the fresh `pr` window + the index; appends close events ONLY to session.jsonl ledgers
    (multi-writer safe). It NEVER writes `requirements.json` — that stays gcampr's single-writer
    segment, so done-ness lives in the ledger (a `session_end` event), not in the index.
      1. `pr_merged` / `pr_closed`: for each of a requirement's branches whose PR is finished in
         the window and not yet recorded, append the event once (dedup by scanning the session).
      2. `session_end`: a requirement with no open branch PR and no session_end yet →
         `done` (some branch merged) / `abandoned` (all finished were closed) / `assumed_done`
         (idle past `stale_after_sec` — the backstop; records "assumed", not a human verdict)."""
    idx = _index(root)
    branches = idx["branches"]
    pr_seg = base.load_segment(root, "pr") or {}
    # branch → its latest PR in the window (highest number wins for a reused branch)
    by_branch: dict[str, dict] = {}
    for p in pr_seg.get("prs", []):
        b = p.get("source_branch")
        if b in branches and p.get("number") is not None:
            if b not in by_branch or p["number"] > by_branch[b].get("number", -1):
                by_branch[b] = p

    for req in list(idx["requirements"]):
        events = session_events(root, req)
        recorded = {e.get("number") for e in events
                    if e.get("kind") in ("pr_merged", "pr_closed")}
        req_branches = [b for b, r in branches.items() if r == req]

        for b in req_branches:
            p = by_branch.get(b)
            if not p or p.get("state") not in ("merged", "closed") or p.get("number") in recorded:
                continue
            record_event(root, req, {"kind": "pr_merged" if p["state"] == "merged" else "pr_closed",
                                     "branch": b, "number": p["number"], "state": p["state"]})
            recorded.add(p["number"])

        if any(e.get("kind") == "session_end" for e in events):
            continue
        states = [by_branch[b]["state"] for b in req_branches if b in by_branch]
        if any(s == "open" for s in states):
            continue                                  # still active — a branch's PR is open
        if any(s == "merged" for s in states):
            result = "done"
        elif states and all(s == "closed" for s in states):
            result = "abandoned"
        elif events and (base.now() - max(e.get("ts", 0) for e in events)) >= stale_after_sec:
            result = "assumed_done"                   # backstop: idle too long, assume delivered
        else:
            continue                                  # in progress (no PR yet, or not stale)
        record_event(root, req, {"kind": "session_end", "result": result})
