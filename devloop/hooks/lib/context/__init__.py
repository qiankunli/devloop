"""`.devloop/` state layer — the bus between collection and use.

Modules group by WHY-THEY-CHANGE-TOGETHER (four families), NOT by storage domain — the
repo/branch/working-tree domain split (see `base.py`) is the FILESYSTEM's axis (where bytes
land, which concurrency invariant holds); a code module like `RepoContext` deliberately spans
two storage domains to present one cohesive view. Families:
- primitives  — `base.py`: shared leaves (`Reference` / `AgentsMd` / `Cadence`) + re-exported
  forge domain (`PullRequest` / `Comment`), constants, time + atomic persistence primitives
  (`load/save_segment`, `append_jsonl`) and the storage-domain resolvers
  (`state_dir` / `worktree_state_dir` / `branch_segment`).
- views       — `repo.py`: `RepoContext`, segment files merged into one OBSERVED/DISPLAY view
  (display-grade PR derivation + two-cadence injection); `workspace.py`: `WorkspaceContext`
  (`context.json`, session cadence) + the `active.json` writer-role.
- truth seams — `gate.py`: `GateView` / `evaluate()`, what hard gates read (LIVE branch +
  SHA-validated PR state, never the cached view; see docs/branch-state.md);
  `prstate.py`: the monitor's & gcampr's shared writer of the monitor-owned segments.
- session runtime — `session.py`: active-repo binding + the checkout owner lock.
- loop-state ledgers — `friction.py` (guard-deny events) + `requirement.py` (requirement
  Trajectory spine): the 经验沉淀 line (workspace docs/loop-state.md). They grow together;
  when a third member lands (steering capture / resolution events), fold the family into a
  `loopstate/` subpackage — rule of three, not before.

Usage (DISPLAY — for injection/hints; a gate must use `gate.evaluate` instead):
    from lib.context import RepoContext, WorkspaceContext, PullRequest
    ctx = RepoContext.load(repo_dir)
    if ctx and ctx.branch_pr_inactive():   # display-grade; gates → lib.context.gate
        ...
"""
from __future__ import annotations

from .base import (
    PRS_CAP,
    REPO_STALE_SEC,
    SESSION_TTL_SEC,
    TURN_TTL_SEC,
    WORKSPACE_STALE_SEC,
    AgentsMd,
    Cadence,
    Comment,
    PullRequest,
    Reference,
    pr_label,
    vocab,
)
from .repo import Branch, BranchTopology, Injection, RepoContext, RepoMeta, Validation
from .session import (
    clear_active_repo,
    load_active_repo,
    record_active_repo,
)
from .workspace import (
    Subproject,
    WorkspaceContext,
    workspace_for_repo,
)
