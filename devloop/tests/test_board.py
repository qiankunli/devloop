#!/usr/bin/env python3
"""Board relevance, compact granular delivery, and per-session cursor behavior."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from _testkit import _git, run_main  # noqa: E402


def _repo(path: str, branch: str = "feat/board"):
    from domain.context import RepoContext

    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    _git(path, "checkout", "-q", "-b", branch)
    Path(path, "f").write_text("x", encoding="utf-8")
    _git(path, "add", "f")
    _git(path, "commit", "-qm", "init")
    return RepoContext.refresh_all(path)


def test_board_delivers_only_changed_items_per_session():
    """One fact changing must not resend every other Board item, and sessions never dedup
    each other. This is the token-saving reason Board owns delivery instead of RepoContext's
    former whole-block cadence."""
    from domain.context import Board, RepoContext

    root = "/tmp/dlut_board_granular"
    ctx = _repo(root)
    first = Board(root, "session-a", repo=ctx).emit()
    assert first and "[Current repo:" in first and "Validation: never run" in first
    assert Board(root, "session-a", repo=RepoContext.load(root)).emit() is None

    Path(root, "dirty").write_text("y", encoding="utf-8")
    changed = Board(root, "session-a", repo=RepoContext.load(root)).emit()
    assert changed and "Workspace: dirty" in changed
    assert "Validation:" not in changed  # unchanged item did not ride the identity update

    other = Board(root, "session-b", repo=RepoContext.load(root)).emit()
    assert other and "Validation: never run" in other  # independent delivery cursor


def test_board_compaction_replays_state_not_review_event():
    """Compaction removes working state from model context, not the fact that a review result
    was already triaged. Board therefore replays state and retains event disposition."""
    from domain.context import Board, BoardSurface, RepoContext, clear_after_compact, store

    root = "/tmp/dlut_board_compact"
    ctx = _repo(root)
    store.save_segment(
        root,
        store.branch_segment(ctx.branch.local.name, "review"),
        {"status": "success", "count": 2, "reviewed_sha": "abcdef1234567", "generated_at": 1.0},
    )
    board = Board(root, "session-a", repo=RepoContext.load(root))
    review = next(item for item in board.items() if item.key == "repo.review")
    assert review.surface is BoardSurface.EVENT
    first = board.emit()
    assert first and "Review: 2 finding(s)" in first
    assert Board(root, "session-a", repo=RepoContext.load(root)).emit() is None

    clear_after_compact(root, "session-a")
    replay = Board(root, "session-a", repo=RepoContext.load(root)).emit()
    assert replay and "[Current repo:" in replay and "Validation:" in replay
    assert "Review:" not in replay


def test_board_injects_current_pr_not_unrelated_recent_window():
    """Prompt Board follows relevance: current PR context is actionable; unrelated recent PRs
    remain available in the fact source for the future UI but do not spend prompt tokens."""
    from domain.context import Board, BoardSurface, PullRequest, RepoContext
    from domain.forge import MergeReadiness

    root = "/tmp/dlut_board_relevant"
    _repo(root)
    ctx = RepoContext.load(root)
    ctx.provider = "github"
    ctx.branch.pr_number = 7
    ctx.merge_readiness = MergeReadiness.CI_BLOCKED.value
    ctx.prs = [
        PullRequest(number=7, state="open", source_branch="feat/board"),
        PullRequest(number=6, state="merged", source_branch="unrelated"),
    ]
    board = Board(root, "session-a", repo=ctx)
    history = next(item for item in board.items() if item.key == "repo.pr-history")
    assert history.surface is BoardSurface.UI_ONLY
    assert len(history.data["pull_requests"]) == 2
    blocked = next(item for item in board.items() if item.key == "repo.pr-blocked")
    assert blocked.surface is BoardSurface.EVENT

    text = board.emit()
    assert "IN-FLIGHT (PR #7" in text
    assert "MERGE-BLOCKED: PR #7" in text
    assert "#6" not in text and "Recent PR/MR history" not in text


if __name__ == "__main__":
    run_main(globals(), label="test_board")
