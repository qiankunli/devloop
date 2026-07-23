#!/usr/bin/env python3
"""SessionStart side effect: best-effort automatic Board HUD inside tmux."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io  # noqa: E402
from ui.board.tmux import ensure_hud_pane  # noqa: E402


def handle(inp: hook_io.HookInput) -> None:
    ensure_hud_pane(inp.cwd, inp.session_id)


if __name__ == "__main__":
    raise SystemExit(hook_io.observe(handle))
