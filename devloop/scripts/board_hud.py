#!/usr/bin/env python3
"""Render Board state for native status lines and the tmux sidecar."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.board import BoardRuntime  # noqa: E402
from lib import config  # noqa: E402
from ui.board.hud import (  # noqa: E402
    HudPulseTracker,
    frame_from_snapshot,
    render_frame,
    render_statusline,
)
from ui.board.tmux import LEADER_ENV, pane_command  # noqa: E402


def _runtime(cwd: str, session_id: str) -> BoardRuntime | None:
    return BoardRuntime.resolve(cwd, session_id)


def _snapshot(cwd: str, session_id: str) -> dict:
    runtime = _runtime(cwd, session_id)
    return runtime.snapshot() if runtime else {"root": cwd, "focus": None, "items": []}


def _watch_text(cwd: str, session_id: str, tracker: HudPulseTracker) -> str | None:
    """Keep the last visible frame when a transient Board read is unavailable."""
    try:
        snapshot = _snapshot(cwd, session_id)
        frame = frame_from_snapshot(snapshot, tracker)
        return render_frame(frame, shutil.get_terminal_size((120, 3)).columns, True)
    except (OSError, ValueError):
        return None


def _shell_commands(environ: dict[str, str] | None = None) -> set[str]:
    environ = os.environ if environ is None else environ
    commands = {"bash", "dash", "fish", "sh", "zsh"}
    configured = Path(environ.get("SHELL", "")).name
    if configured:
        commands.add(configured)
    return commands


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render devloop's Board HUD")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--claude-statusline", action="store_true")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--session-id", default=os.environ.get("DEVLOOP_HUD_SESSION", ""))
    parser.add_argument("--leader-pane", default=os.environ.get(LEADER_ENV, ""))
    return parser.parse_args()


def main() -> int:
    args = _args()
    if args.claude_statusline:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            return 0
        workspace = payload.get("workspace") if isinstance(payload, dict) else None
        cwd = (
            workspace.get("current_dir")
            if isinstance(workspace, dict) and workspace.get("current_dir")
            else payload.get("cwd") if isinstance(payload, dict) else None
        ) or args.cwd
        session_id = (
            str(payload.get("session_id") or args.session_id)
            if isinstance(payload, dict)
            else args.session_id
        )
        if not config.board_hud(cwd).get("enabled", True):
            return 0
        runtime = _runtime(cwd, session_id)
        if runtime is None:
            return 0
        columns = os.environ.get("COLUMNS", "")
        width = int(columns) if columns.isdigit() else shutil.get_terminal_size((120, 2)).columns
        frame = frame_from_snapshot(runtime.snapshot())
        print(render_statusline(frame, max(1, width - 4), not os.environ.get("NO_COLOR")))
        return 0
    if args.json:
        print(json.dumps(_snapshot(args.cwd, args.session_id), indent=2, ensure_ascii=False))
        return 0
    if not args.watch:
        frame = frame_from_snapshot(_snapshot(args.cwd, args.session_id))
        print(render_frame(frame, shutil.get_terminal_size((120, 3)).columns, sys.stdout.isatty()))
        return 0

    stopped = False

    def stop(_signum=None, _frame=None):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    tracker = HudPulseTracker()
    inactive_leader_ticks = 0
    shell_commands = _shell_commands()
    sys.stdout.write("\x1b[?25l\x1b[2J\x1b[H")
    sys.stdout.flush()
    try:
        while not stopped:
            if args.leader_pane:
                leader_command = pane_command(args.leader_pane)
                if leader_command is None:
                    break
                inactive_leader_ticks = (
                    inactive_leader_ticks + 1
                    if Path(leader_command).name in shell_commands
                    else 0
                )
                if inactive_leader_ticks >= 3:
                    break
            text = _watch_text(args.cwd, args.session_id, tracker)
            if text is not None:
                lines = "\n".join("\x1b[2K" + line for line in text.splitlines())
                sys.stdout.write("\x1b[H" + lines + "\x1b[J")
                sys.stdout.flush()
            time.sleep(1)
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
