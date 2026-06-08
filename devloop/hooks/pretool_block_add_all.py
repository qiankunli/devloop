#!/usr/bin/env python3
"""PreToolUse (Bash): deny `git add -A` / `--all` / `.` — force explicit staging."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io  # noqa: E402
from lib.cmdtree import cmdparse  # noqa: E402

_ADD_ALL_ARGS = {"-A", "--all", ".", "./"}


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    # Parsed (not regex): catches `git -C repo add -A`, ignores quoted occurrences.
    for inv in cmdparse.git_invocations(inp.command):
        if inv["subcommand"] == "add" and (_ADD_ALL_ARGS & set(inv["args"])):
            return (
                "⚠️  Refusing `git add -A` / `git add --all` / `git add .`.\n"
                "These globs often capture IDE configs (.idea/), env files (.env), or unrelated "
                "plan / scratch files. Stage files explicitly:\n"
                "  git add path/to/file1 path/to/file2\n"
                "Or use the plugin's `smart_gcam.sh` / `stage_and_commit` flow which skips sensitive files."
            )
    return None


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
