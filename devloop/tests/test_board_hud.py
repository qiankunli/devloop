#!/usr/bin/env python3
"""Board HUD fixed-line projection, pulse replacement, and tmux lifecycle."""
from __future__ import annotations

import subprocess

from _testkit import _hook_input, _load_hook, run_main  # noqa: E402


def _item(item_type: str, payload: dict, revision: str) -> dict:
    return {
        "id": f"/repo:{item_type}",
        "revision": revision,
        "type": item_type,
        "kind": "state",
        "scope": {"workspace_root": "/ws", "repo_root": "/repo"},
        "payload": payload,
    }


def _snapshot(*items: dict) -> dict:
    return {
        "root": "/ws",
        "focus": {"workspace_root": "/ws", "repo_root": "/repo"},
        "items": list(items),
    }


def _identity(revision: str = "i1", **changes) -> dict:
    payload = {
        "code_dir": "/repo/devloop",
        "branch": "feat/board-ui",
        "ahead": 1,
        "behind": 0,
        "modified_count": 2,
        "untracked_count": 0,
        "pr_label": "PR #110",
        "pr_state": "open",
    }
    payload.update(changes)
    return _item("repo.identity", payload, revision)


def test_hud_renders_three_semantic_lines():
    from ui.board.hud import HudPulseTracker, frame_from_snapshot, render_frame

    snapshot = _snapshot(
        _identity(),
        _item(
            "requirement.current",
            {"source": "requirement.current", "text": "Requirement: Board UI"},
            "r1",
        ),
        _item(
            "repo.validation",
            {"components": [{"component": ".", "lint_at": 1, "test_at": 2}]},
            "v1",
        ),
        _item(
            "repo.review",
            {"status": "success", "reviewed_sha": "abc", "findings": 2, "failed_files": 0},
            "rv1",
        ),
    )
    rendered = render_frame(
        frame_from_snapshot(snapshot, HudPulseTracker()),
        width=120,
        color=False,
    )
    lines = rendered.splitlines()
    assert len(lines) == 3
    assert "req:Board UI" in lines[0] and "repo:devloop" in lines[0]
    assert "feat/board-ui" in lines[1] and "PR #110:open" in lines[1]
    assert "validation:✓" in lines[1] and "review:2 findings" in lines[1]
    assert lines[2] == "watching Board"


def test_hud_pulse_is_latest_change_and_critical_state_stays_stable():
    from ui.board.hud import HudPulseTracker, frame_from_snapshot, render_frame

    tracker = HudPulseTracker()
    base = _snapshot(
        _identity(modified_count=0),
        _item("repo.validation", {"components": []}, "v1"),
    )
    frame_from_snapshot(base, tracker, now=1)

    dirty = _snapshot(
        _identity("i2", modified_count=3),
        _item("repo.validation", {"components": []}, "v1"),
    )
    first = render_frame(frame_from_snapshot(dirty, tracker, now=2), color=False)
    assert "working tree changed · 3 files" in first.splitlines()[2]

    blocked = _snapshot(
        _identity("i2", modified_count=3),
        _item("repo.validation", {"components": []}, "v1"),
        _item("repo.pr-blocked", {"label": "PR #110", "readiness": "ci_blocked"}, "b1"),
    )
    second = render_frame(frame_from_snapshot(blocked, tracker, now=3), color=False)
    lines = second.splitlines()
    assert "BLOCKED:ci_blocked" in lines[1]
    assert "merge blocked · ci_blocked" in lines[2]

    review = _snapshot(
        _identity("i2", modified_count=3),
        _item("repo.validation", {"components": []}, "v1"),
        _item("repo.pr-blocked", {"label": "PR #110", "readiness": "ci_blocked"}, "b1"),
        _item(
            "repo.review",
            {"status": "success", "reviewed_sha": "abc", "findings": 2, "failed_files": 0},
            "rv1",
        ),
    )
    third = render_frame(frame_from_snapshot(review, tracker, now=4), color=False)
    lines = third.splitlines()
    assert "BLOCKED:ci_blocked" in lines[1]
    assert "review success · 2 findings" in lines[2]


def test_hud_never_wraps_and_sanitizes_dynamic_text():
    from ui.board.hud import frame_from_snapshot, render_frame

    snapshot = _snapshot(
        _identity(branch="feat/very-long\x1b[31m-branch-name"),
        _item(
            "requirement.current",
            {"text": "Requirement: 一个非常长的 requirement title that cannot fit"},
            "r1",
        ),
    )
    lines = render_frame(frame_from_snapshot(snapshot), width=24, color=False).splitlines()
    assert len(lines) == 3
    assert all(len(line) <= 24 for line in lines)
    assert all("\x1b" not in line for line in lines)


class _FakeTmux:
    def __init__(self, panes: str = ""):
        self.panes = panes
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == "display-message":
            return subprocess.CompletedProcess(args, 0, "80\n", "")
        if args[0] == "list-panes":
            return subprocess.CompletedProcess(args, 0, self.panes, "")
        if args[0] == "split-window":
            return subprocess.CompletedProcess(args, 0, "%9\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_tmux_hud_creates_then_reuses_owned_three_line_pane():
    from lib import config
    from ui.board import tmux

    assert (config.plugin_root() / "scripts/board_hud.py").is_file()
    env = {"TMUX": "/tmp/tmux", "TMUX_PANE": "%1"}
    fake = _FakeTmux()
    assert tmux.ensure_hud_pane("/repo", "session-a", env=env, run_tmux=fake) == "created"
    split = next(call for call in fake.calls if call[0] == "split-window")
    assert split[split.index("-l") + 1] == "3"
    command = split[-1]
    assert "DEVLOOP_HUD_OWNER=1" in command
    assert "DEVLOOP_HUD_SESSION=session-a" in command
    assert "--leader-pane %1" in command

    existing = (
        "%8\texec env DEVLOOP_HUD_OWNER=1 DEVLOOP_HUD_SESSION=session-a "
        "DEVLOOP_HUD_LEADER_PANE=%1 python board_hud.py --watch\n"
    )
    reused = _FakeTmux(existing)
    assert tmux.ensure_hud_pane("/repo", "session-a", env=env, run_tmux=reused) == "reused"
    assert any(call[:3] == ["resize-pane", "-t", "%8"] for call in reused.calls)
    assert not any(call[0] == "split-window" for call in reused.calls)

    stale = _FakeTmux(
        "%7\texec env DEVLOOP_HUD_OWNER=1 DEVLOOP_HUD_SESSION=old-session "
        "DEVLOOP_HUD_LEADER_PANE=%1 python board_hud.py --watch\n"
    )
    assert tmux.ensure_hud_pane("/repo", "session-a", env=env, run_tmux=stale) == "created"
    assert ["kill-pane", "-t", "%7"] in stale.calls


def test_sessionstart_hud_hook_is_best_effort_observer():
    hook = _load_hook("board_hud_start")
    seen = []
    original = hook.ensure_hud_pane
    try:
        hook.ensure_hud_pane = lambda cwd, session_id: seen.append((cwd, session_id))
        assert hook.handle(_hook_input("", {"cwd": "/ws", "session_id": "s1"})) is None
    finally:
        hook.ensure_hud_pane = original
    assert seen == [("/ws", "s1")]


if __name__ == "__main__":
    run_main(globals())
