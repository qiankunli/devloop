#!/usr/bin/env python3
"""Codex SessionStart wrapper.

Claude's SessionStart supports `watchPaths`, which devloop uses for AGENTS.md
FileChanged wiring. Codex currently rejects that field in SessionStart output,
so this wrapper reuses the same prewarm/additionalContext producer and strips
the Claude-only watcher payload before emitting JSON.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sessionstart_init  # noqa: E402
from lib import hook_io  # noqa: E402


def build(inp: hook_io.HookInput) -> dict | None:
    payload = sessionstart_init.build(inp)
    if not payload:
        return None
    payload = dict(payload)
    payload.pop("watchPaths", None)
    return payload or None


if __name__ == "__main__":
    raise SystemExit(hook_io.run(build, event="SessionStart"))
