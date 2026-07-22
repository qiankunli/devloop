"""`.devloop/` context layer — fact sources plus the prompt-facing Board.

Modules group by WHY-THEY-CHANGE-TOGETHER (four families), NOT by storage domain — the
repo/branch/working-tree domain split (see `store.py`) is the FILESYSTEM's axis (where bytes
land, which concurrency invariant holds); a code module like `RepoContext` deliberately spans
two storage domains to present one cohesive view. Families:
- primitives  — `store.py`: the shared DISK — storage-domain resolvers (`state_dir` /
  `worktree_state_dir` / `branch_segment` / `tmp_dir`) + the two persistence disciplines
  (`save_segment` overwrite / `append_jsonl` ledger); `base.py`: the shared VOCABULARY —
  leaves (`Reference` / `AgentsMd` / `Cadence`), re-exported forge domain, constants, time.
- views       — `repo.py`: `RepoContext`, segment files merged into one OBSERVED/DISPLAY view;
  `workspace.py`: `WorkspaceContext` (`context.json`); `board.py`: relevant structured
  items + one surface policy + per-session delivery cursors.
- truth seams — `gate.py`: `GateView` / `evaluate()`, what hard gates read (LIVE branch +
  SHA-validated PR state, never the cached view; see docs/branch-state.md);
  `prstate.py`: the monitor's & gcampr's shared writer of the monitor-owned segments.
- session runtime — `session.py`: active-repo binding + the checkout owner lock.
- loop-state ledgers — `loopstate/` subpackage: `friction` (guard-deny events) +
  `requirement` (requirement Trajectory spine), the 经验沉淀 line (workspace
  docs/loop-state.md). Future members: steering capture, resolution events, the miner.

Usage (DISPLAY facts; Board owns prompt delivery, gates use `gate.evaluate`):
    from domain.context import RepoContext, WorkspaceContext, PullRequest
    ctx = RepoContext.load(repo_dir)
    if ctx and ctx.branch_pr_inactive():   # display-grade; gates → domain.context.gate
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
from .board import Board, BoardItem, BoardSurface, clear_after_compact, clear_session
from .repo import Branch, BranchTopology, RepoContext, RepoMeta, Validation
from .session import (
    clear_active_repo,
    load_active_repo,
    load_active_repo_lenient,
    record_active_repo,
    record_session_event,
)
from .workspace import (
    Subproject,
    WorkspaceContext,
    workspace_for_repo,
)
