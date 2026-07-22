#!/usr/bin/env python3
"""PostCompact: make Board replay state items, but never consumed events/nudges."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io
from domain.context import Board, clear_after_compact  # noqa: E402


def handle(inp: hook_io.HookInput) -> None:
    board = Board.resolve(inp.cwd, inp.session_id)
    if board:
        clear_after_compact(board.root, inp.session_id)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
