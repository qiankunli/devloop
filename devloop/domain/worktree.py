"""Managed git worktree lifecycle used by the `/enter` flow.

All devloop-created worktrees live below ``<repo>/.worktrees/``. Keeping creation,
reuse, pruning, and dependency preparation behind this module prevents callers from
reproducing only the visible ``git worktree add`` step and skipping lifecycle policy.
"""
from __future__ import annotations

import os
from pathlib import Path

from lib import config, ecosystem, git_state, gitcmd

from . import repo_layout
from .context import session


def prepare_environment(path: str) -> list[str]:
    """Prepare every component in a new/reused worktree; return environment warnings."""
    warnings = []
    for component in repo_layout.discover_components(path):
        if problem := ecosystem.ensure_ready(component.path):
            warnings.append(f"component {component.id}: {problem}")
    return warnings


def create_or_reuse(repo_dir: str, tag: str) -> tuple[str | None, str]:
    """Create or reuse ``.worktrees/<tag>`` from ``origin/<target>``.

    Worktrees live inside the repo, never as siblings. The legacy ``worktrees/`` layout
    remains readable so existing checkouts continue to resolve. The operation is
    idempotent; each call also prunes old managed worktrees and prepares dependencies.
    """
    base = Path(repo_dir)
    rel = Path(".worktrees") / tag
    for legacy in (rel, Path("worktrees") / tag):
        if (base / legacy).is_dir():
            path = str((base / legacy).resolve())
            _prune_old(repo_dir, keep_path=path)
            warnings = prepare_environment(path)
            msg = "reused existing worktree"
            if warnings:
                msg += "; environment warning: " + " | ".join(warnings)
            return path, msg

    target = git_state.local_default_target(repo_dir)
    branch = f"worktree-{tag}"
    result = gitcmd.git(repo_dir, "worktree", "add", "-b", branch,
                        str(rel), f"origin/{target}", timeout=30)
    if not result.ok:
        retry = gitcmd.git(repo_dir, "worktree", "add", str(rel), branch, timeout=30)
        if not retry.ok:
            return None, f"worktree add failed: {result.err or retry.err}"

    path = str((base / rel).resolve())
    _prune_old(repo_dir, keep_path=path)
    warnings = prepare_environment(path)
    msg = "created worktree"
    if warnings:
        msg += "; environment warning: " + " | ".join(warnings)
    return path, msg


def _activity(path: str) -> float:
    """Rank by checkout-local directory/index activity, not shared commit time.

    Worktrees branched from the same trunk share a baseline commit, so commit time would
    flatten their ordering. The per-worktree index changes on add/commit/checkout/switch.
    """
    times = []
    try:
        times.append(os.stat(path).st_mtime)
    except OSError:
        pass
    result = gitcmd.git(path, "rev-parse", "--git-path", "index")
    if result.ok and result.out:
        try:
            times.append(os.stat(Path(path) / result.out).st_mtime)
        except OSError:
            pass
    return max(times) if times else 0.0


def _managed(repo_dir: str) -> list[str]:
    """Return only linked worktrees directly below devloop's managed homes.

    External or sibling worktrees created by a human are intentionally excluded and are
    never pruning targets.
    """
    worktrees = git_state.list_worktrees(repo_dir)
    if not worktrees:
        return []
    main = Path(worktrees[0][0]).resolve()
    homes = {main / ".worktrees", main / "worktrees"}
    return [
        str(Path(path).resolve())
        for path, _sha, _branch in worktrees[1:]
        if Path(path).resolve().parent in homes
    ]


def _prune_old(repo_dir: str, keep_path: str | None = None) -> None:
    """Best-effort prune old managed worktrees while preserving live work.

    ``keep_recent`` semantics: positive keeps the N most active, zero removes every
    surplus checkout, negative disables pruning. Removal is non-force, so dirty
    worktrees survive; ``keep_path`` and checkouts owned by another live session are
    skipped. Branches are retained, allowing a later enter to rebuild the checkout.
    """
    keep = config.worktree(repo_dir).get("keep_recent", 5)
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 5
    if keep < 0:
        return
    managed = _managed(repo_dir)
    if len(managed) <= keep:
        return
    protected = str(Path(keep_path).resolve()) if keep_path else None
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    doomed = sorted(managed, key=_activity, reverse=True)[keep:]
    pruned = False
    for path in doomed:
        if path == protected:
            continue
        if sid and session.foreign_owner(path, sid):
            continue
        if gitcmd.git(repo_dir, "worktree", "remove", path, timeout=30).ok:
            pruned = True
    if pruned:
        gitcmd.git(repo_dir, "worktree", "prune", timeout=15)
