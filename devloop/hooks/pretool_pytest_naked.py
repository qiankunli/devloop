#!/usr/bin/env python3
"""PreToolUse (Bash): deny bare `pytest` when the repo has a `make test` target.

Bare pytest often collects 0 items (PYTHONPATH=. missing); the Makefile sets it up.
Allows an explicit env prefix (`PYTHONPATH=. pytest ...`) — detected by inspecting the
segment BEFORE env-stripping.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import cmdparse, hook_io, repo_layout  # noqa: E402
from lib.context import RepoContext  # noqa: E402


def _is_naked_pytest(seg: list[str]) -> bool:
    """seg = one segment's tokens (env NOT stripped). True if it's a bare pytest
    invocation with no leading env assignment."""
    if not seg:
        return False
    if "=" in seg[0] and not seg[0].startswith("-"):
        return False  # env prefix present (e.g. PYTHONPATH=.) → allowed
    toks = seg
    if toks[0] == "uv" and "run" in toks[:2]:
        toks = toks[2:]
    base = os.path.basename(toks[0]) if toks else ""
    if base == "pytest":
        return True
    if base.startswith("python") and toks[1:3] == ["-m", "pytest"]:
        return True
    return False


def decide(inp: hook_io.HookInput) -> str | None:
    if not inp.is_tool("Bash"):
        return None
    if not any(_is_naked_pytest(seg) for seg in cmdparse.segments(inp.command)):
        return None
    git_root = repo_layout.find_git_root(inp.cwd)
    if not git_root:
        return None
    ctx = RepoContext.load(git_root)
    code_dir = ctx.repo.code_dir if ctx else git_root
    if not _has_make_test(code_dir):
        return None
    return (
        "⚠️  Bare pytest often fails (PYTHONPATH=. missing → collected 0 items).\n"
        f"Use the Makefile target:  cd {code_dir} && make test\n"
        f"Single-case debug:  cd {code_dir} && PYTHONPATH=. .venv/bin/python -m pytest <path> -k <case>"
    )


def _has_make_test(code_dir: str) -> bool:
    makefile = Path(code_dir) / "Makefile"
    if not makefile.exists():
        return False
    try:
        return bool(re.search(r"^test(-\w+)?\s*:", makefile.read_text(encoding="utf-8"), re.MULTILINE))
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(hook_io.guard(decide))
