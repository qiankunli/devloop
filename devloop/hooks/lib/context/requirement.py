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
    the path). Stamps `ts`; caller supplies `kind` + payload."""
    base.append_jsonl(root, f"{_INDEX}/{requirement_id}/session",
                      {"ts": round(base.now(), 1), **event})


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
