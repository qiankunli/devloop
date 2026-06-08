"""`.devloop/` state layer — the bus between collection and use.

Read in this order:
- `base.py`  — shared leaves (`Reference` / `AgentsMd` / `WorktreeInfo` / `Cadence`) plus
  the re-exported forge domain (`PullRequest` / `Comment`), constants, time + atomic
  persistence primitives (`load/save_segment`).
- `repo.py`  — `RepoContext`: per-owner segment files (`meta`/`branch`/`pr`/`validation`/
  `injection`.json) merged into one view; PR derivation (`current_pr` /
  `branch_pr_inactive`) + two-cadence injection.
- `workspace.py` — `WorkspaceContext`: `context.json` (session-cadence only) plus the
  `active.json` segment (last-active repo, the "activity" writer-role).

Usage:
    from lib.context import RepoContext, WorkspaceContext, PullRequest
    ctx = RepoContext.load(repo_dir)
    if ctx and ctx.branch_pr_inactive():
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
    WorktreeInfo,
    vocab,
)
from .repo import BranchState, Injection, RepoContext, RepoMeta, Validation
from .workspace import (
    Subproject,
    WorkspaceContext,
    load_active_repo,
    record_active_repo,
    workspace_for_repo,
)
