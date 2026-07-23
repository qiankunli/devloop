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
    from domain.board import BoardRuntime
    from domain.context import RepoContext

    root = "/tmp/dlut_board_granular"
    ctx = _repo(root)
    first = BoardRuntime.from_facts(root, "session-a", repo=ctx).deliver_prompt()
    assert first and "[Current repo:" in first and "Validation: never run" in first
    assert BoardRuntime.from_facts(
        root, "session-a", repo=RepoContext.load(root)
    ).deliver_prompt() is None

    Path(root, "dirty").write_text("y", encoding="utf-8")
    changed = BoardRuntime.from_facts(
        root, "session-a", repo=RepoContext.load(root)
    ).deliver_prompt()
    assert changed and "Workspace: dirty" in changed
    assert "Validation:" not in changed  # unchanged item did not ride the identity update

    other = BoardRuntime.from_facts(
        root, "session-b", repo=RepoContext.load(root)
    ).deliver_prompt()
    assert other and "Validation: never run" in other  # independent delivery cursor


def test_board_compaction_replays_state_not_review_event():
    """Compaction removes working state from model context, not the fact that a review result
    was already triaged. Board therefore replays state and retains event disposition."""
    from domain.board import BoardItemKind, BoardItemType, BoardRuntime
    from domain.context import RepoContext, store

    root = "/tmp/dlut_board_compact"
    ctx = _repo(root)
    store.save_segment(
        root,
        store.branch_segment(ctx.branch.local.name, "review"),
        {"status": "success", "count": 2, "reviewed_sha": "abcdef1234567", "generated_at": 1.0},
    )
    board = BoardRuntime.from_facts(root, "session-a", repo=RepoContext.load(root))
    review = next(item for item in board.view.items if item.type is BoardItemType.REPO_REVIEW)
    assert review.kind is BoardItemKind.EVENT
    first = board.deliver_prompt()
    assert first and "Review: 2 finding(s)" in first
    assert BoardRuntime.from_facts(
        root, "session-a", repo=RepoContext.load(root)
    ).deliver_prompt() is None

    board.after_compact()
    replay = BoardRuntime.from_facts(
        root, "session-a", repo=RepoContext.load(root)
    ).deliver_prompt()
    assert replay and "[Current repo:" in replay and "Validation:" in replay
    assert "Review:" not in replay


def test_board_injects_current_pr_not_unrelated_recent_window():
    """Prompt Board follows relevance: current PR context is actionable; unrelated recent PRs
    remain available in the fact source for the future UI but do not spend prompt tokens."""
    from domain.board import (
        BoardItemType,
        BoardItemKind,
        BoardRuntime,
        DeliveryChannel,
        DeliveryPolicy,
    )
    from domain.context import PullRequest, RepoContext
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
    board = BoardRuntime.from_facts(root, "session-a", repo=ctx)
    history = next(item for item in board.view.items if item.type is BoardItemType.REPO_PR_HISTORY)
    assert DeliveryPolicy.rule_for(history.type).channels == frozenset({DeliveryChannel.UI})
    assert len(history.payload.pull_requests) == 2
    blocked = next(item for item in board.view.items if item.type is BoardItemType.REPO_PR_BLOCKED)
    assert blocked.kind is BoardItemKind.EVENT

    text = board.deliver_prompt()
    assert "IN-FLIGHT (PR #7" in text
    assert "MERGE-BLOCKED: PR #7" in text
    assert "#6" not in text and "Recent PR/MR history" not in text


def test_board_snapshot_is_structured_and_does_not_consume_prompt_delivery():
    """The UI reads typed Board facts; prompt text is only one renderer and has its own receipt."""
    from domain.board import BoardRuntime

    root = "/tmp/dlut_board_snapshot"
    board = BoardRuntime.from_facts(root, "session-a", repo=_repo(root))
    snapshot = board.snapshot()

    assert snapshot["focus"]["repo_root"] == root
    identity = next(item for item in snapshot["items"] if item["type"] == "repo.identity")
    assert identity["kind"] == "state"
    assert identity["revision"]
    assert identity["payload"]["branch"] == "feat/board"
    assert "text" not in identity

    # A UI read is side-effect free: the first prompt delivery is still due.
    assert "[Current repo:" in board.deliver_prompt()


def test_shared_board_scopes_views_and_receipts_per_repo():
    """A shared Board may hold multiple repos; focus and delivery identity must not collide."""
    from domain.board import BoardFocus, PromptDelivery, project_board

    root = "/tmp/dlut_board_multi"
    shutil.rmtree(root, ignore_errors=True)
    left = _repo(f"{root}/left", "feat/left")
    right = _repo(f"{root}/right", "feat/right")
    board = project_board(root, repos=(left, right))
    left_view = board.view(BoardFocus(root, left.repo.repo_dir))
    right_view = board.view(BoardFocus(root, right.repo.repo_dir))

    left_ids = {item.id for item in left_view.items}
    right_ids = {item.id for item in right_view.items}
    assert left_ids.isdisjoint(right_ids)
    assert all(item.scope.repo_root == left.repo.repo_dir for item in left_view.items)
    assert all(item.scope.repo_root == right.repo.repo_dir for item in right_view.items)

    delivery = PromptDelivery(root, "session-a")
    assert "feat/left" in delivery.deliver(left_view)
    assert "feat/right" in delivery.deliver(right_view)


if __name__ == "__main__":
    run_main(globals())
