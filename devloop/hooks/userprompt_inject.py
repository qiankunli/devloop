#!/usr/bin/env python3
"""UserPromptSubmit: inject the relevant, changed parts of the shared Board."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io
from domain.context import (  # noqa: E402
    Board,
    record_session_event,
)


def produce(inp: hook_io.HookInput) -> str | None:
    board = Board.resolve(inp.cwd, inp.session_id)
    text = board.emit() if board else None
    # Log what actually went out (session.record_session_event). Placed here, not around each
    # emit, because what's worth reviewing is the ASSEMBLED block the model saw. `text` only —
    # NOT the user's prompt: the point is reviewing what WE emit, and the user's words are
    # already in the CLI transcript, so copying them would duplicate them into a second,
    # unaudited place. Needs git_root: the log is repo-domain, and a workspace-only turn has no
    # repo to file it under. Fail-open — observability must never cost the injection itself.
    if board and board.repo and text:
        try:
            record_session_event(board.repo.repo.repo_dir, inp.session_id, "inject", text=text)
        except Exception:
            pass
    return text


if __name__ == "__main__":
    raise SystemExit(hook_io.inject(produce, event="UserPromptSubmit"))
