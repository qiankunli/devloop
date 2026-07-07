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

from ...forge import ForgeError, forge_for_repo
from .. import base, store

_INDEX = "requirements"   # segment name → .devloop/requirements.json + session dir root


def _index(root) -> dict:
    idx = store.load_segment(root, _INDEX) or {}
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
    store.append_jsonl(root, f"{_INDEX}/{requirement_id}/session", {"ts": base.now(), **event})


def open_requirement(root, branch: str, *, fork_from: str | None = None,
                     fork_sha: str | None = None) -> str:
    """Start a new requirement whose id = `branch`. Indexes the branch and emits `session_start`.

    Idempotent while the current arc is ACTIVE (no duplicate session_start). A same-name branch
    arriving AFTER the requirement closed is a NEW delivery arc: append a fresh `session_start`
    delimiter — readers (and `reconcile_closures`) split arcs on session_end, so without this the
    second arc's events would dangle behind the old closure, invisible. Returns the id."""
    idx = _index(root)
    if idx["branches"].get(branch) == branch:
        if _active_tail(session_events(root, branch)):
            return branch          # current arc still active → idempotent no-op
        record_event(root, branch, {"kind": "session_start", "requirement": branch,
                                    "branch": branch, "fork_from": fork_from, "fork_sha": fork_sha})
        return branch
    idx["branches"][branch] = branch
    idx["requirements"][branch] = {"status": "open", "ts": round(base.now(), 1)}
    store.save_segment(root, _INDEX, idx)
    record_event(root, branch, {"kind": "session_start", "requirement": branch, "branch": branch,
                                "fork_from": fork_from, "fork_sha": fork_sha})
    return branch


def attach_branch(root, requirement_id: str, branch: str, *, fork_sha: str | None = None) -> None:
    """A subsequent branch joins requirement `requirement_id`. Indexes it and emits
    `branch_cut{continues:true}`. No-op if already attached to that requirement.

    Guards the arc invariant first: every arc must OPEN with a `session_start` delimiter —
    readers and `reconcile_closures` split arcs on session_start/session_end. Two attach paths
    would otherwise violate it: `--requirement <never-opened id>` on the continue path makes
    branch_cut the spine's FIRST line, and attaching after closure (post-merge follow-up)
    leaves branch_cut dangling behind `session_end` — invisible to `_active_tail`, so the
    follow-up's arc would never be reconciled."""
    idx = _index(root)
    if idx["branches"].get(branch) == requirement_id:
        return
    idx["branches"][branch] = requirement_id
    idx["requirements"].setdefault(requirement_id, {"status": "open", "ts": round(base.now(), 1)})
    store.save_segment(root, _INDEX, idx)
    if not _active_tail(session_events(root, requirement_id)):   # empty spine or closed arc
        record_event(root, requirement_id, {"kind": "session_start", "requirement": requirement_id,
                                            "branch": requirement_id, "fork_from": None,
                                            "fork_sha": None})
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
    p = store.state_dir(root) / _INDEX / requirement_id / "session.jsonl"
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


def _active_tail(events: list[dict]) -> list[dict]:
    """The current ARC: events after the last `session_end` ([] = closed/empty). Arcs delimit
    deliveries on a reused name — closure is judged per-arc, never against a previous arc."""
    last_end = max((i for i, e in enumerate(events) if e.get("kind") == "session_end"), default=-1)
    return events[last_end + 1:]


def reconcile_closures(root, *, stale_after_sec: float = base.REQUIREMENT_STALE_SEC) -> None:
    """Close finished requirements from the monitor side — best-effort, idempotent,
    LEVEL-triggered (safe to run every tick / on restart / after a missed tick).

    Appends ONLY to session.jsonl ledgers; NEVER writes `requirements.json` (gcampr's
    single-writer segment) — done-ness lives in the ledger (`session_end`), not the index.

    Closure is EVENT-SOURCED, keyed on the spine's own `pr_created` numbers — not on matching
    window branches — for two reasons: (a) the pr window is capped (PRS_CAP): a PR that merged
    while no monitor ran can fall off it, so off-window numbers get one authoritative
    `forge.get(number)` fallback; (b) a reused branch name's OLD merged PR must never close the
    NEW arc — a number recorded in this arc can't be confused with last arc's. Branch-level
    window matching survives only for branches with no `pr_created` anywhere in the spine
    (a PR raised outside gcampr), where the ambiguity is unresolvable anyway.
      1. `pr_merged` / `pr_closed`: for each spine `pr_created` number not yet recorded, state
         from the window, else from the forge (only numbers we KNOW exist are ever fetched —
         no polling for never-raised PRs); append once.
      2. `session_end` on the current arc: `done` (a PR merged) / `abandoned` (all finished
         PRs were closed) / `assumed_done` (idle past `stale_after_sec` — the backstop; an
         assumption on record, not a human verdict). A known-open PR keeps the arc open."""
    idx = _index(root)
    branches = idx["branches"]
    pr_seg = store.load_segment(root, "pr") or {}
    win_by_num: dict[int, str] = {}
    win_by_branch: dict[str, dict] = {}    # legacy path: branch → latest window PR
    for p in pr_seg.get("prs", []):
        n, b = p.get("number"), p.get("source_branch")
        if n is None:
            continue
        win_by_num[n] = p.get("state", "")
        if b and (b not in win_by_branch or n > win_by_branch[b]["number"]):
            win_by_branch[b] = {"number": n, "state": p.get("state", "")}

    forge = None                            # lazy, one client per reconcile pass

    def pr_state(num: int) -> str | None:
        """State of PR `num`: window first, else one forge GET (F2 fallback). None = unknown."""
        nonlocal forge
        if num in win_by_num:
            return win_by_num[num]
        if forge is None:
            forge = forge_for_repo(root) or False
        if not forge:
            return None
        try:
            return forge.get(num).state
        except ForgeError:
            return None

    for req in list(idx["requirements"]):
        events = session_events(root, req)
        created: dict[int, str | None] = {}     # spine pr_created: number → branch
        recorded: set[int] = set()
        for e in events:
            k, n = e.get("kind"), e.get("number")
            if k == "pr_created" and n is not None:
                created[n] = e.get("branch")
            elif k in ("pr_merged", "pr_closed") and n is not None:
                recorded.add(n)

        active_open = False
        # 1a. number-first: finished spine PRs → pr_merged / pr_closed
        for num, br in created.items():
            if num in recorded:
                continue
            state = pr_state(num)
            if state == "open":
                active_open = True
                continue
            if state not in ("merged", "closed"):
                continue                             # unknown → leave for staleness backstop
            ev = {"ts": base.now(), "kind": "pr_merged" if state == "merged" else "pr_closed",
                  "branch": br, "number": num, "state": state}
            record_event(root, req, ev)
            events.append(ev)
            recorded.add(num)
        # 1b. legacy: branches with no pr_created anywhere → window branch-match (old behavior)
        spine_created_branches = {b for b in created.values() if b}
        for b in (b for b, r in branches.items() if r == req and b not in spine_created_branches):
            w = win_by_branch.get(b)
            if not w or w["number"] in recorded:
                continue
            if w["state"] == "open":
                active_open = True
                continue
            if w["state"] not in ("merged", "closed"):
                continue
            ev = {"ts": base.now(), "kind": "pr_merged" if w["state"] == "merged" else "pr_closed",
                  "branch": b, "number": w["number"], "state": w["state"]}
            record_event(root, req, ev)
            events.append(ev)
            recorded.add(w["number"])

        # 2. close the current arc
        tail = _active_tail(events)
        if not tail or active_open:
            continue
        tail_finished = [e for e in tail if e.get("kind") in ("pr_merged", "pr_closed")]
        pending = {e["number"] for e in tail if e.get("kind") == "pr_created"
                   and e.get("number") is not None} - {e.get("number") for e in tail_finished}
        stale = (base.now() - max(e.get("ts", 0) for e in tail)) >= stale_after_sec
        if pending:                                   # PRs whose state we couldn't resolve
            if stale:
                record_event(root, req, {"kind": "session_end", "result": "assumed_done"})
            continue
        if any(e.get("kind") == "pr_merged" for e in tail_finished):
            result = "done"
        elif tail_finished:
            result = "abandoned"
        elif stale:
            result = "assumed_done"
        else:
            continue
        record_event(root, req, {"kind": "session_end", "result": result})
