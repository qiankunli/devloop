#!/usr/bin/env python3
"""PreToolUse (Bash): deny subproject-level commands at an aggregate workspace root.

Running `make` / `uv` / `pytest` / `go` / `npm` etc. at the workspace root (not a
git repo) will fail or misbehave — tell the user to cd into a subproject first.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import cmdparse, hook_io, workspace  # noqa: E402
from lib.context import WorkspaceContext, load_active_repo  # noqa: E402

_SUBPROJECT_CMDS = {"make", "uv", "pytest", "go", "npm", "pnpm", "yarn", "cargo"}


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    cmds = cmdparse.commands(inp.command)
    if not any(os.path.basename(c[0]) in _SUBPROJECT_CMDS for c in cmds):
        return None
    # `cd <sub> && <cmd>` is fine — a cd segment moves into a real repo first.
    if any(c and os.path.basename(c[0]) == "cd" for c in cmds):
        return None
    cwd_resolved = str(Path(inp.cwd).resolve())
    if cwd_resolved not in {str(Path(w).resolve()) for w in workspace.load_workspaces()}:
        return None
    ws = WorkspaceContext.load(cwd_resolved)
    subs = [s.name.strip("`") for s in (ws.subprojects if ws else [])[:10] if s.name]
    hint = ("\nRegistered subprojects: " + ", ".join(subs)) if subs else ""
    active = load_active_repo(cwd_resolved)
    active_hint = f"\nLast-active subproject: {active}" if active else ""
    return (
        f"⚠️  You're at the workspace root '{cwd_resolved}', not inside a subproject.\n"
        "Running a subproject-level command here will fail or misbehave.\n"
        "Either `cd <subproject>` (or /enter <subproject>) first, or use the devloop scripts, "
        "which resolve the repo themselves (smart_gcam* accept --repo <name|path>; "
        "run_fixlint/run_tests take it as the first argument)."
        f"{hint}{active_hint}"
    )


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
