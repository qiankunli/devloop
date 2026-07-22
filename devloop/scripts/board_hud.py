#!/usr/bin/env python3
"""One-shot/JSON/watch entrypoint for the Board's fixed three-line terminal HUD."""
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
from ui.board.hud import HudPulseTracker, frame_from_snapshot, render_frame  # noqa: E402
from ui.board.tmux import LEADER_ENV, pane_command  # noqa: E402


def _snapshot(cwd: str, session_id: str) -> dict:
    runtime = BoardRuntime.resolve(cwd, session_id)
    return runtime.snapshot() if runtime else {"root": cwd, "focus": None, "items": []}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render devloop's three-line Board HUD")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--session-id", default=os.environ.get("DEVLOOP_HUD_SESSION", ""))
    parser.add_argument("--leader-pane", default=os.environ.get(LEADER_ENV, ""))
    return parser.parse_args()


def main() -> int:
    args = _args()
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
    shell_commands = {"bash", "dash", "fish", "sh", "zsh"}
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
            snapshot = _snapshot(args.cwd, args.session_id)
            frame = frame_from_snapshot(snapshot, tracker)
            text = render_frame(frame, shutil.get_terminal_size((120, 3)).columns, True)
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
