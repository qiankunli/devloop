#!/usr/bin/env python3
"""PreToolUse (Bash): deny `git commit` when the lint gate is on and lint is stale.

Driven by the `precommit` section of `~/.devloop/config.json` (default off).
Last-line fallback for repos that want strict enforcement; the normal flow runs lint
before commit anyway.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import config, hook_io, repo_layout  # noqa: E402
from lib.cmdtree import cmdparse  # noqa: E402
from lib.context import RepoContext  # noqa: E402


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    # Judge each commit against ITS OWN repo (the `-C <dir>` target or cwd), mirroring
    # pretool_protect_branch — `git -C subrepo commit` fired from a workspace root
    # (where cwd isn't a git repo) must hit subrepo's gate, not silently pass.
    for inv in cmdparse.git_invocations(inp.command):
        if inv.subcommand != "commit":
            continue
        git_root = repo_layout.find_git_root(inv.run_dir(inp.cwd))
        if not git_root:
            continue
        if not _repo_config(git_root).get("commit_gate_lint", False):
            continue
        ctx = RepoContext.load(git_root)
        stale = ctx.validation.edits_since_lint if ctx else 0
        last = ctx.validation.last_lint_at if ctx else None
        if stale == 0 and last:
            continue
        parts = ["⚠️  Refusing `git commit`: precommit gate enabled and lint is stale."]
        if not last:
            parts.append("Lint has never run for this branch.")
        if stale:
            parts.append(f"{stale} edit(s) since last lint pass.")
        parts.append("Run /lint (and /test if your repo requires it), then retry commit.")
        parts.append("Disable this gate for the repo under `precommit` in ~/.devloop/config.json.")
        return "\n".join(parts)
    return None


def _repo_config(git_root: str) -> dict:
    cfg = config.precommit(git_root)
    default = cfg.get("default") or {}
    repos = cfg.get("repos") or {}
    repo_abs = str(Path(git_root).resolve())
    for key, val in repos.items():
        if str(Path(key).expanduser().resolve()) == repo_abs:
            return {**default, **(val or {})}
    return dict(default)


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
