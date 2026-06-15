"""PR / remote-branch state acquisition — the shared library the monitor AND gcampr use to
pull the forge + the server's trunk tips into the monitor-owned segments.

It lives in `lib` (not the monitor *script*) so the gate path (`lib.context.gate`) and gcampr
can trigger an AUTHORITATIVE refresh without importing a script — the old arrangement had the
poll logic in `scripts/poll_pr_status.py` and gcampr reached back into it, then discarded the
result (a silent no-op). Both writers go through here.

Two monitor-owned segments, both stamped so a reader can tell how fresh they are:
- `pr.json` — the current branch's PR/MR number (SHA-ancestry validated) + a recent window.
- `remote_branches.json` — the server's trunk tips (`{name, commit}`) + `fetched_at`, the
  read-freshness baseline (a colleague's push moves trunk under you, an unobservable channel).
"""
from __future__ import annotations

from dataclasses import asdict

from .. import git_state
from ..forge import ForgeError, build_window, forge_for_repo
from . import base

# Conventional trunk names to track remote tips for. ls-remote returns only those that exist, so
# tracking all of them (a) survives `origin/HEAD` pointing at a dead placeholder and (b) covers
# repos with more than one protected branch (e.g. release + master). The repo's actual baseline
# (its `target` / recorded `fork_from`, which may be `develop` / `release/x` / `staging`) is
# unioned in by `_baseline_branches` so the staleness signal isn't limited to these defaults.
TRUNK_CANDIDATES = ("main", "master", "release")


def _baseline_branches(repo: str) -> tuple[str, ...]:
    """The branches whose remote tip matters for THIS repo: the conventional trunks plus the
    configured `target` and the recorded `fork_from` (read from the branch segment). Without this
    a non-conventional baseline would have no tracked tip and the 'trunk moved' signal would never
    fire for it (Codex P2)."""
    bseg = base.load_segment(repo, "branch") or {}
    extra = (bseg.get("target"), (bseg.get("local") or {}).get("fork_from"))
    seen: set[str] = set()
    out: list[str] = []
    for b in (*TRUNK_CANDIDATES, *(e for e in extra if e)):
        if b not in seen:
            seen.add(b)
            out.append(b)
    return tuple(out)


# ── PR selection (SHA-ancestry validated; reused by the monitor AND the gate) ──
def pick_branch_pr(branch_prs: list, repo: str, head_sha: str):
    """Choose the PR/MR that represents the branch at `head_sha` (or None).

    Open PR wins (branch reused for new work). Otherwise the most-recent finished PR whose
    source SHA is reachable from HEAD — a dead-ref PR (same branch NAME, unrelated history,
    e.g. delete+rebuild) is skipped so it can't mark a rebuilt branch inactive. Running this
    SHA check against the LIVE head is why a gate keys PR-ownership on (branch, head_sha), not
    branch name alone (see docs/branch-state.md §write-gate)."""
    opens = [p for p in branch_prs if p.is_open]
    if opens:
        return opens[0]
    for p in branch_prs:                       # forge.list() returns created desc
        if git_state.is_ancestor(repo, p.sha, head_sha):
            return p
    return None


# ── pr.json (the current branch's PR + recent window) ──────────────────────────
def poll_pr(repo: str) -> dict | None:
    """One forge poll → the `pr` segment payload (current branch's PR window), or None when
    the repo has no usable forge / remote. Side-effect-free; `persist_pr` writes it."""
    forge = forge_for_repo(repo)
    if forge is None:
        return None
    branch = git_state.get_current_branch(repo)
    head = git_state.get_head_sha(repo)
    try:
        branch_pr = pick_branch_pr(forge.prs_for_branch(branch), repo, head) if branch else None
        anchor = branch_pr.number if branch_pr else None
        window = build_window(forge, anchor)
    except ForgeError:
        return None
    # Readiness is a derived verdict over source×target tips (goes stale when either moves), so we
    # compute it live HERE per poll rather than storing it on each PullRequest — see MergeReadiness.
    # Only for the current branch's OPEN MR (a finished/absent PR has nothing to nag about); its own
    # guard so a readiness-fetch failure degrades to "unknown", not a lost window.
    readiness = None
    if branch_pr and branch_pr.is_open:
        try:
            readiness = forge.merge_readiness(anchor).value
        except ForgeError:
            readiness = None
    return {
        "branch": branch,
        "head_sha": head,          # provenance: the HEAD this window was selected against
        "provider": forge.provider,
        "pr_number": anchor,
        "merge_readiness": readiness,   # current branch's open MR; None when no open MR / unknown
        "prs": [asdict(p) for p in window],
    }


def persist_pr(repo: str, payload: dict) -> None:
    """Write the monitor-owned `pr` segment (sole writer; no lock, no lost update)."""
    git_state.ensure_gitignore_excluded(repo)   # keep /.devloop/ out of git if pr.json is first
    base.save_segment(repo, "pr", payload)


def refresh_pr(repo: str) -> bool:
    """Poll + persist the `pr` segment in one shot — the authoritative refresh a low-frequency
    gate (gcampr) triggers so it decides on LIVE PR state, not a possibly-stale monitor cache.
    (The old `refresh_pr_state` polled and DISCARDED the result — a silent no-op.) Best-effort;
    returns whether anything was written."""
    payload = poll_pr(repo)
    if payload is None:
        return False
    persist_pr(repo, payload)
    return True


# ── remote_branches.json (the server's trunk tips; read-freshness baseline) ────
def poll_remote_branches(repo: str, branches: tuple[str, ...] = TRUNK_CANDIDATES) -> dict | None:
    """`git ls-remote` the candidate trunk branches → the `remote_branches` payload, or None
    offline. No object fetch (cheap); the SHAs are the TRUE remote tips — a colleague's push is
    visible here before any local fetch, which is the whole point of polling them."""
    tips = git_state.ls_remote_tips(repo, *branches)
    if not tips:
        return None
    return {
        "fetched_at": base.now(),
        "remotes": [{"name": name, "commit": sha} for name, sha in sorted(tips.items())],
    }


def persist_remote_branches(repo: str, payload: dict) -> None:
    """Write the monitor-owned `remote_branches` segment (sole writer)."""
    git_state.ensure_gitignore_excluded(repo)
    base.save_segment(repo, "remote_branches", payload)


def refresh_remote_branches(repo: str, branches: tuple[str, ...] | None = None) -> bool:
    """Poll + persist the server's trunk tips. Tracks the repo's actual baseline (target /
    fork_from) on top of the conventional trunks unless `branches` is given. Best-effort;
    returns whether written."""
    payload = poll_remote_branches(repo, branches or _baseline_branches(repo))
    if payload is None:
        return False
    persist_remote_branches(repo, payload)
    return True
