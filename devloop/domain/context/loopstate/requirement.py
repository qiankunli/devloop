"""Requirement scope — the loop-state unit of accumulation: ONE requirement's delivery, from
its first branch cut to PR/MR merge, spanning 1..N branches **across 1..N repos**. See
workspace docs/requirement-first.md (target state) + docs/loop-state.md (events / mining).

The requirement domain lives at the **dev root** — the workspace root when the repo belongs
to a registered aggregate workspace, else the main repo root (single-repo mode is the
degenerate case where every branch happens to live in one repo; same shapes, no second
mechanism). One requirement has ONE spine: events from every participating repo append to
the same ledger and carry a `repo` field, so cross-repo state needs no name-join at read time.

Two artifacts, two writer disciplines:
- `<dev_root>/.devloop/requirements.json` — the **index segment**
  `{branches: {repo: {branch: requirement_id}}, requirements: {id: {status, ts}}}`.
  A SEGMENT (whole-file overwrite); its writer-role is gcampr. With multiple repos sharing
  one index, two gcampr runs in different repos could race the read-modify-write — accepted:
  same role, single human, worst case one lost branch mapping that `note()`'s lazy-open heals.
- `<dev_root>/.devloop/requirements/<id>/session.jsonl` — the requirement's **session
  ledger** (append-only lifecycle spine). O_APPEND single-line appends stay multi-writer
  safe across repos. Named after the requirement's FIRST branch — an ID, not a description.

`repo` in the index/events is the resolved MAIN-repo path: machine-local by nature (the
whole `.devloop` domain is per-clone, gitignored), and it doubles as the join key back to
that repo's `pr.json` window for the live view — zero reverse lookup.

Friction/resolution events stay in their own per-repo flat ledgers keyed by branch/sha and
JOIN to a requirement via this index at mine time — the hot deny path never reads the index.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from domain.forge import ForgeError
from lib.forge import forge_for_repo
from .. import base, store

_INDEX = "requirements"   # segment name → .devloop/requirements.json + session dir root


@lru_cache(maxsize=64)
def _dev_root(repo_root: str) -> str:
    """The requirement domain's home: workspace root if `repo_root` belongs to a registered
    workspace, else the main repo root. The Mode A/B split is absorbed HERE — callers and
    storage shapes above never see two modes."""
    from ..workspace import workspace_for_repo   # deferred: workspace joins on this grain too
    try:
        ws = workspace_for_repo(repo_root)
    except Exception:
        ws = None
    return ws or str(store._main_repo_root(str(repo_root)))


def _repo_key(repo_root) -> str:
    """A repo's identity in the index/events: its REALPATH'd main-repo path. Worktrees fold
    into their main checkout (same as the repo state domain); realpath dedupes spellings —
    workspaces are symlink farms, so the same repo is reachable as both the symlink and the
    canonical path, and two spellings must not split one repo into two index keys."""
    p = store._main_repo_root(str(repo_root))
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def _index(dev: str) -> dict:
    idx = store.load_segment(dev, _INDEX) or {}
    idx.setdefault("branches", {})       # repo → {branch → requirement_id}
    idx.setdefault("requirements", {})   # requirement_id → {status, ts}
    return idx


def resolve(root, branch: str) -> str | None:
    """The requirement id a branch OF THIS REPO belongs to, or None if never attached
    (e.g. created outside gcampr)."""
    return _index(_dev_root(str(root)))["branches"].get(_repo_key(root), {}).get(branch)


def record_event(root, requirement_id: str, event: dict) -> None:
    """Append one event to the requirement's spine at the dev root. Stamps a FULL-precision
    `ts` — not rounded: the close backstop does `now - ts` staleness math, and a rounded-up
    ts could exceed `now` and flip the age negative. Caller supplies `kind` + payload
    (including `repo` for repo-scoped events; requirement-global events carry none)."""
    store.append_jsonl(_dev_root(str(root)), f"{_INDEX}/{requirement_id}/session",
                       {"ts": base.now(), **event})


def open_requirement(root, branch: str, *, fork_from: str | None = None,
                     fork_sha: str | None = None) -> str:
    """Start a new requirement whose id = `branch`. Indexes the branch and emits `session_start`.

    Idempotent while the current arc is ACTIVE (no duplicate session_start). A same-name branch
    arriving AFTER the requirement closed is a NEW delivery arc: append a fresh `session_start`
    delimiter — readers (and `reconcile_closures`) split arcs on session_end, so without this the
    second arc's events would dangle behind the old closure, invisible. Returns the id."""
    dev, repo = _dev_root(str(root)), _repo_key(root)
    idx = _index(dev)
    if idx["branches"].get(repo, {}).get(branch) == branch:
        if _active_tail(session_events(root, branch)):
            return branch          # current arc still active → idempotent no-op
        record_event(root, branch, {"kind": "session_start", "requirement": branch,
                                    "repo": repo, "branch": branch,
                                    "fork_from": fork_from, "fork_sha": fork_sha})
        return branch
    idx["branches"].setdefault(repo, {})[branch] = branch
    idx["requirements"][branch] = {"status": "open", "ts": round(base.now(), 1)}
    store.save_segment(dev, _INDEX, idx)
    record_event(root, branch, {"kind": "session_start", "requirement": branch, "repo": repo,
                                "branch": branch, "fork_from": fork_from, "fork_sha": fork_sha})
    return branch


def attach_branch(root, requirement_id: str, branch: str, *, fork_sha: str | None = None) -> None:
    """A subsequent branch (of THIS repo) joins requirement `requirement_id` — the cross-repo
    case is just calling this from another repo. Indexes it and emits `branch_cut
    {continues:true}`. No-op if already attached to that requirement.

    Guards the arc invariant first: every arc must OPEN with a `session_start` delimiter —
    readers and `reconcile_closures` split arcs on session_start/session_end. Two attach paths
    would otherwise violate it: `--requirement <never-opened id>` on the continue path makes
    branch_cut the spine's FIRST line, and attaching after closure (post-merge follow-up)
    leaves branch_cut dangling behind `session_end` — invisible to `_active_tail`, so the
    follow-up's arc would never be reconciled."""
    dev, repo = _dev_root(str(root)), _repo_key(root)
    idx = _index(dev)
    if idx["branches"].get(repo, {}).get(branch) == requirement_id:
        return
    idx["branches"].setdefault(repo, {})[branch] = requirement_id
    idx["requirements"].setdefault(requirement_id, {"status": "open", "ts": round(base.now(), 1)})
    store.save_segment(dev, _INDEX, idx)
    if not _active_tail(session_events(root, requirement_id)):   # empty spine or closed arc
        record_event(root, requirement_id, {"kind": "session_start",
                                            "requirement": requirement_id, "repo": repo,
                                            "branch": requirement_id, "fork_from": None,
                                            "fork_sha": None})
    record_event(root, requirement_id, {"kind": "branch_cut", "repo": repo, "branch": branch,
                                        "continues": True, "fork_sha": fork_sha})


def note(root, branch: str, event: dict) -> None:
    """Append `event` to the requirement `branch` (of this repo) belongs to, stamping the
    repo key. Lazily opens a requirement (id = branch) if the branch isn't indexed yet — so
    an event (e.g. pr_created) on a branch created outside gcampr still lands somewhere
    sensible."""
    req = resolve(root, branch) or open_requirement(root, branch)
    record_event(root, req, {"repo": _repo_key(root), **event})


# ── derived live view: the turn-injection requirement segment ─────────────────
def turn_line(root, branch: str | None) -> str:
    """One-line 'where is my TASK' view for turn injection: the requirement this branch
    belongs to + live state of every PR the requirement raised (across repos).

    Derived read-only — spine `pr_created` joined against each repo's `pr.json` window;
    nothing is persisted and the forge is NEVER hit on this hot path (off-window PRs render
    as `?`). Empty string when the branch has no requirement or its arc already closed —
    zero tokens unless there is an actual task in flight."""
    if not branch:
        return ""
    try:
        req = resolve(root, branch)
        if not req:
            return ""
        tail = _active_tail(session_events(root, req))
        if not tail:
            return ""
        created: list[tuple[str, int]] = []          # (repo, number) in spine order
        repos_seen: set[str] = set()
        for e in tail:
            if e.get("kind") in ("session_start", "branch_cut", "pr_created"):
                repos_seen.add(e.get("repo") or _repo_key(root))
            if e.get("kind") == "pr_created" and e.get("number") is not None:
                pair = (e.get("repo") or _repo_key(root), e["number"])
                if pair not in created:
                    created.append(pair)
        if not created:
            branches = {(e.get("repo"), e.get("branch")) for e in tail
                        if e.get("kind") in ("session_start", "branch_cut")}
            return f"Requirement: {req} ({len(branches)} branch(es), no PR yet)"
        multi = len({r for r, _ in created}) > 1
        win: dict[str, dict[int, str]] = {}          # repo → {number → state}, lazy per repo
        parts = []
        for repo, num in created[-base.REQ_VIEW_PRS_CAP:]:
            if repo not in win:
                seg = store.load_segment(repo, "pr") or {}
                win[repo] = {p.get("number"): p.get("state", "?")
                             for p in seg.get("prs", []) if p.get("number") is not None}
            state = win[repo].get(num, "?")          # off-window → unknown; never hit the forge
            prefix = f"{Path(repo).name}" if multi else ""
            parts.append(f"{prefix}#{num} {state}")
        return f"Requirement: {req} | PRs: " + " · ".join(parts)
    except Exception:
        return ""    # derived view — must never break turn injection


# ── close half: monitor-side scope closing ───────────────────────────────────
def session_events(root, requirement_id: str) -> list[dict]:
    """Parse a requirement's session.jsonl into events. Fail-open ([]) on missing/corrupt —
    a close reconcile must never raise inside the monitor sweep."""
    p = store.state_dir(_dev_root(str(root))) / _INDEX / requirement_id / "session.jsonl"
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
    LEVEL-triggered (safe to run every tick / on restart / after a missed tick). Any repo's
    monitor reconciles the whole dev root: spines are shared, so whichever repo polls first
    closes for everyone (a concurrent duplicate append is possible and tolerated — readers
    key finished PRs by (repo, number), a doubled line changes nothing).

    Appends ONLY to session.jsonl ledgers; NEVER writes `requirements.json` (gcampr's
    single-writer segment) — done-ness lives in the ledger (`session_end`), not the index.

    Closure is EVENT-SOURCED, keyed on the spine's own `pr_created` (repo, number) pairs —
    numbers are only unique per forge project, so the repo is part of the key. Window state
    comes from EACH event's own repo (`pr.json` loaded lazily per repo); off-window numbers
    get one authoritative `forge.get(number)` against that repo (only PRs we KNOW exist are
    ever fetched). Branch-level window matching survives only for branches with no
    `pr_created` anywhere in the spine (a PR raised outside gcampr).
      1. `pr_merged` / `pr_closed`: for each spine pr_created not yet recorded, state from
         its repo's window, else its repo's forge; append once.
      2. `session_end` on the current arc: `done` (a PR merged) / `abandoned` (all finished
         PRs were closed) / `assumed_done` (idle past `stale_after_sec` — the backstop; an
         assumption on record, not a human verdict). A known-open PR keeps the arc open."""
    dev = _dev_root(str(root))
    self_repo = _repo_key(root)
    idx = _index(dev)
    branches = idx["branches"]

    win_num: dict[str, dict[int, str]] = {}        # repo → {number → state}
    win_branch: dict[str, dict[str, dict]] = {}    # repo → {branch → latest window PR}
    forges: dict[str, object] = {}                 # repo → forge client | False

    def _windows(repo: str) -> tuple[dict[int, str], dict[str, dict]]:
        if repo not in win_num:
            by_num: dict[int, str] = {}
            by_branch: dict[str, dict] = {}
            for p in (store.load_segment(repo, "pr") or {}).get("prs", []):
                n, b = p.get("number"), p.get("source_branch")
                if n is None:
                    continue
                by_num[n] = p.get("state", "")
                if b and (b not in by_branch or n > by_branch[b]["number"]):
                    by_branch[b] = {"number": n, "state": p.get("state", "")}
            win_num[repo], win_branch[repo] = by_num, by_branch
        return win_num[repo], win_branch[repo]

    def pr_state(repo: str, num: int) -> str | None:
        """State of `repo`'s PR `num`: window first, else one forge GET. None = unknown."""
        by_num, _ = _windows(repo)
        if num in by_num:
            return by_num[num]
        if repo not in forges:
            forges[repo] = forge_for_repo(repo) or False
        f = forges[repo]
        if not f:
            return None
        try:
            return f.get(num).state
        except ForgeError:
            return None

    for req in list(idx["requirements"]):
        events = session_events(root, req)
        created: dict[tuple[str, int], str | None] = {}   # (repo, number) → branch
        recorded: set[tuple[str, int]] = set()
        for e in events:
            k, n = e.get("kind"), e.get("number")
            repo = e.get("repo") or self_repo
            if k == "pr_created" and n is not None:
                created[(repo, n)] = e.get("branch")
            elif k in ("pr_merged", "pr_closed") and n is not None:
                recorded.add((repo, n))

        active_open = False
        # 1a. number-first: finished spine PRs → pr_merged / pr_closed
        for (repo, num), br in created.items():
            if (repo, num) in recorded:
                continue
            state = pr_state(repo, num)
            if state == "open":
                active_open = True
                continue
            if state not in ("merged", "closed"):
                continue                             # unknown → leave for staleness backstop
            ev = {"ts": base.now(), "kind": "pr_merged" if state == "merged" else "pr_closed",
                  "repo": repo, "branch": br, "number": num, "state": state}
            record_event(root, req, ev)
            events.append(ev)
            recorded.add((repo, num))
        # 1b. legacy: branches with no pr_created anywhere → window branch-match (old behavior)
        spine_created = {(r, created[(r, n)]) for (r, n) in created if created[(r, n)]}
        for repo, mapping in branches.items():
            _, by_branch = _windows(repo)
            for b in (b for b, r in mapping.items() if r == req and (repo, b) not in spine_created):
                w = by_branch.get(b)
                if not w or (repo, w["number"]) in recorded:
                    continue
                if w["state"] == "open":
                    active_open = True
                    continue
                if w["state"] not in ("merged", "closed"):
                    continue
                ev = {"ts": base.now(),
                      "kind": "pr_merged" if w["state"] == "merged" else "pr_closed",
                      "repo": repo, "branch": b, "number": w["number"], "state": w["state"]}
                record_event(root, req, ev)
                events.append(ev)
                recorded.add((repo, w["number"]))

        # 2. close the current arc
        tail = _active_tail(events)
        if not tail or active_open:
            continue
        tail_finished = [e for e in tail if e.get("kind") in ("pr_merged", "pr_closed")]
        fin_keys = {(e.get("repo") or self_repo, e.get("number")) for e in tail_finished}
        pending = {(e.get("repo") or self_repo, e["number"]) for e in tail
                   if e.get("kind") == "pr_created" and e.get("number") is not None} - fin_keys
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
