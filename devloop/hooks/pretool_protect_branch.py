#!/usr/bin/env python3
"""PreToolUse (Bash): deny `git commit` / `git push` on a protected branch."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout  # noqa: E402
from lib.cmdtree import cmdparse  # noqa: E402
from lib.context import gate  # noqa: E402


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    # Parsed (not regex): catches `git -C repo commit`, ignores quoted text. Each
    # commit/push is judged against ITS OWN target repo (the `-C <dir>` or cwd), so a
    # protected-branch commit on `git -C subprojectB` is caught even from the workspace
    # root (where cwd itself isn't a git repo) — Codex finding #4.
    for inv in cmdparse.git_invocations(inp.command):
        if inv.subcommand not in ("commit", "push"):
            continue
        git_root = repo_layout.find_git_root(inv.run_dir(inp.cwd))
        if not git_root:
            continue
        # gate.evaluate reads the LIVE branch (git rev-parse), never the cached segment: a
        # checkout via an unobserved channel (subshell, make, another terminal) onto a
        # protected branch must not slip through because the cache still says feature.
        gv = gate.evaluate(git_root)
        if not gv.protected():
            continue
        where = f" in repo '{Path(git_root).name}'" if inv.dash_c else ""
        return (
            f"⚠️  Refusing `git commit/push` on protected branch '{gv.branch or '?'}'{where}.\n"
            f"Create a feature branch first: `git checkout -b <name> origin/{gv.target}` "
            f"(or use /gcampr to do it properly)."
        )
    return None


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
