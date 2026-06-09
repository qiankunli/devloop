"""`.devloop/` state layer — the bus between collection and use.

Read in this order:
- `base.py`  — shared leaves (`Reference` / `AgentsMd` / `Cadence`) plus the re-exported forge
  domain (`PullRequest` / `Comment`), constants, time + atomic persistence primitives
  (`load/save_segment`).
- `repo.py`  — `RepoContext`: per-owner segment files (`meta`/`branch`/`remote_branches`/`pr`/
  `validation`/`injection`.json) merged into one OBSERVED/DISPLAY view (`Branch` /
  `BranchTopology`); display-grade PR derivation + two-cadence injection.
- `gate.py`  — `GateView` / `evaluate()`: the gate-truth seam. Hard gates (protect / merged
  guards, gcampr) read facts here — LIVE branch + SHA-validated PR state — NOT the cached
  `RepoContext`. See its docstring + docs/branch-state.md for why the two are separate.
- `prstate.py` — the monitor's & gcampr's shared writer of the monitor-owned segments
  (`pr.json` PR window, `remote_branches.json` trunk tips), with SHA-ancestry PR selection.
- `workspace.py` — `WorkspaceContext`: `context.json` (session-cadence only) plus the
  `active.json` segment (last-active repo, the "activity" writer-role).

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
from .workspace import (
    Subproject,
    WorkspaceContext,
    load_active_repo,
    record_active_repo,
    workspace_for_repo,
)
