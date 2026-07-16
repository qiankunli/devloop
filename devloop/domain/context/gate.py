"""Gate truth — the ONE place a hard gate obtains branch / PR facts, resolved at DECISION
time, never trusting the cached `RepoContext` snapshot.

Why a separate seam from `RepoContext` (the observed/display view): a gate blocks or allows an
outward, hard-to-reverse action (commit / push / edit-on-a-dead-branch). `RepoContext.branch`
is refreshed by *observed* events (cd, parseable git commands), so after an unobserved checkout
(a subshell `cd "$var" && git checkout`, `make`, another terminal) its `branch.local.name` can
be stale — and a gate keyed on it either fails OPEN (allows a push to a protected branch the
cache still thinks is a feature branch) or fails CLOSED (blocks edits on a branch whose PR the
cache still thinks is merged). So every hard gate routes through `evaluate()` here instead.
The CI invariant test (`test_gates_use_gate_seam`) enforces that no guard reads
`ctx.branch.*`/`ctx.branch_pr_inactive()` for a decision.

Cost tiers by gate frequency (see docs/branch-state.md):
- branch identity (name + HEAD): cheap, volatile → always LIVE (`git rev-parse`).
- PR activeness: forge data is expensive → reuse the monitor-cached window, but VALIDATE it
  against the LIVE branch + LIVE HEAD via the SHA-ancestry picker (local merge-base, NO forge
  call). The low-frequency push gate (gcampr) passes `live_refresh=True` for a synchronous
  authoritative forge poll first.
"""
from __future__ import annotations

from dataclasses import dataclass

from lib import git_state
from . import prstate, store
from .base import PullRequest


@dataclass(frozen=True)
class GateView:
    """Authoritative, decision-time facts for one repo. Built by `evaluate()`; the fields are
    LIVE git reads, `active_pr` is the monitor-cached PR validated against live HEAD."""
    git_root: str
    branch: str | None        # LIVE current branch (None on detached HEAD)
    head_sha: str             # LIVE HEAD sha
    target: str               # canonical trunk (stable → from the cached branch segment)
    provider: str
    active_pr: PullRequest | None

    def protected(self) -> bool:
        return git_state.is_protected_branch(self.branch)

    def inactive(self) -> bool:
        """The current branch's PR/MR is merged/closed — block edits / refuse to continue."""
        return bool(self.active_pr and self.active_pr.inactive)

    def in_flight(self) -> bool:
        """The current branch's PR/MR is open / awaiting human merge."""
        return bool(self.active_pr and self.active_pr.is_open)


def evaluate(git_root: str, *, live_refresh: bool = False) -> GateView:
    """Resolve the gate-truth facts for `git_root` ONCE (one set of live git reads).

    `live_refresh=True` (gcampr, low-frequency) runs an authoritative forge poll+persist first
    so the decision sees server-fresh PR state; the edit-frequency guards omit it and rely on
    the monitor-maintained window (still correct in the BLOCK direction — see `active_pr`)."""
    if live_refresh:
        prstate.refresh_pr(git_root)   # poll + PERSIST (the old refresh_pr_state discarded this)

    branch = git_state.get_current_branch(git_root)
    head = git_state.get_head_sha(git_root)

    # PR activeness: take the monitor-cached window (no forge call on the hot path) but select
    # the branch's PR against the LIVE branch + LIVE HEAD. pick_branch_pr's SHA-ancestry check
    # means a stale window only ever fails to FIND the PR (→ no block) — it can't resurrect a
    # merged PR for a branch HEAD no longer points at. So the edit guards stay cheap AND
    # correct in the block direction.
    pr_seg = store.load_segment(git_root, "pr") or {}
    prs = [PullRequest.from_dict(p) for p in (pr_seg.get("prs") or []) if p.get("number") is not None]
    branch_prs = [p for p in prs if p.source_branch == branch] if branch else []
    active_pr = prstate.pick_branch_pr(branch_prs, git_root, head)

    # target is stable (the repo's trunk) → read the cached repo fact (meta.default_branch, the
    # single source); only identity must be live. Fall back to a live local derive if uninitialized.
    mseg = store.load_segment(git_root, "meta") or {}
    target = (mseg.get("repo") or {}).get("default_branch") or git_state.local_default_target(git_root)

    return GateView(
        git_root=str(git_root),
        branch=branch,
        head_sha=head,
        target=target,
        provider=pr_seg.get("provider", ""),
        active_pr=active_pr,
    )
