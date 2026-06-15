#!/usr/bin/env python3
"""Run `make fix` then the lint target in the repo's code dir; stamp lint on success.

Encodes all the case logic (repo resolution, code_dir detection, target choice,
no-target skip, marking) so the skill/command markdown just calls this and trusts
the output. Only `make fix` may modify files — this script never edits code itself.

The stamped result must be authoritative — two observed false-green classes are
closed here rather than documented around:
- **CI-entry drift**: a repo's `lint-ci` typically `uv sync`s the pinned toolchain
  first; running plain `lint` with a newer local formatter passes locally and fails
  in CI. Prefer `lint-ci` when the Makefile has it.
- **warm-cache lies**: a stale `.mypy_cache` has reported green on a tree that a
  cold run flags moments later. Clear it before linting — slower, but a stamp that
  can ship a broken MR is worse than a slow one.

Usage: run_fixlint.py [--repo R | R]   (R = a path or a workspace subproject name;
default = cwd's repo, falling back to the workspace's last-active repo)
Exit: 0 lint passed or cleanly skipped; 1 lint failed (output shown).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import cli  # noqa: E402
from lib.context import RepoContext, record_active_repo  # noqa: E402


def has_target(code_dir: str, name: str) -> bool:
    mk = Path(code_dir) / "Makefile"
    if not mk.exists():
        return False
    try:
        return bool(re.search(rf"^{re.escape(name)}\s*:", mk.read_text(encoding="utf-8"), re.MULTILINE))
    except OSError:
        return False


def pick_lint_target(code_dir: str) -> str | None:
    """Prefer the CI lint entry when the repo has one (see module docstring)."""
    for t in ("lint-ci", "lint"):
        if has_target(code_dir, t):
            return t
    return None


def run_make(code_dir: str, target: str) -> int:
    print(f"--- make {target} (cwd={code_dir}) ---")
    return subprocess.run(["make", target], cwd=code_dir).returncode


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(prog="run_fixlint.py", description="make fix + lint; stamp on pass.")
    cli.add_repo_arg(ap)
    ns = ap.parse_args(argv)
    resolved, how = cli.resolve_repo_or_exit(ns, "run_fixlint")
    repo, code_dir = resolved.git_root, resolved.code_dir  # path identities live in ResolvedRepo
    if how != "cwd":
        print(f"run_fixlint: repo = {repo} ({how})")
    record_active_repo(repo)
    ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)

    lint_target = pick_lint_target(code_dir)
    if lint_target is None:
        print(f"run_fixlint: no `make lint`/`make lint-ci` target in {code_dir} — skipping (nothing to verify).")
        return 0

    if has_target(code_dir, "fix"):
        run_make(code_dir, "fix")   # may modify files; rc ignored (fixers can be non-zero)

    shutil.rmtree(Path(code_dir) / ".mypy_cache", ignore_errors=True)
    rc = run_make(code_dir, lint_target)
    if rc == 0:
        ctx.mark_lint_passed()
        print(f"✓ lint passed (make {lint_target}) — stamped .devloop validation.")
        return 0
    print(f"✗ lint failed (make {lint_target}) — fix the reported issues (only `make fix` may edit files).")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
