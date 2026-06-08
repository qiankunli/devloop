#!/usr/bin/env python3
"""PreToolUse (Bash): deny `pip install` in uv-managed repos. Allows `pip install -e .`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import hook_io, repo_layout  # noqa: E402
from lib.cmdtree import cmdparse  # noqa: E402
from lib.context import RepoContext  # noqa: E402


def _pip_install_args(toks: list[str]) -> list[str] | None:
    """If `toks` is a pip install invocation, return the args after 'install', else None."""
    base = os.path.basename(toks[0]) if toks else ""
    if base in ("pip", "pip3") and "install" in toks[1:]:
        return toks[toks.index("install") + 1:]
    if base.startswith("python") and toks[1:3] == ["-m", "pip"] and "install" in toks[3:]:
        return toks[toks.index("install") + 1:]
    return None


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    installs = [a for a in (_pip_install_args(c) for c in cmdparse.commands(inp.command)) if a is not None]
    if not installs:
        return None
    # Allow when every pip-install is the local-dev `pip install -e .` form.
    if all("-e" in a and "." in a for a in installs):
        return None
    git_root = repo_layout.find_git_root(inp.cwd)
    if not git_root:
        return None
    ctx = RepoContext.load(git_root)
    code_dir = Path(ctx.repo.code_dir if ctx else git_root)
    if not ((code_dir / "pyproject.toml").exists() and (code_dir / "uv.lock").exists()):
        return None
    return (
        "⚠️  This repo is uv-managed (pyproject.toml + uv.lock).\n"
        "Don't use `pip install` directly. Instead:\n"
        "  uv add <package>    # add a dependency\n"
        "  uv sync             # install / update from pyproject.toml\n"
        "`pip install -e .` is allowed for local dev installs."
    )


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
