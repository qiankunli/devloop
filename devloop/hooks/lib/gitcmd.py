"""Unified git runner — the single source for every git subprocess call in devloop.

Why this exists (and why it is NOT GitPython):
- Hooks run on the hot path (every tool call). A git call that raises would break
  the user's tool call, so every call here is **failure-safe**: timeout / missing
  git / any OSError → `GitResult(rc=-1, ...)`, never an exception.
- It is **timeout-guarded** by default (git over a dead network must not hang a hook).
- Zero dependency, offline-safe. GitPython would add a hot-path import, an
  exception model we'd have to re-wrap, and gives no help with the genuinely hard
  part (GitLab MR semantics). So we wrap the `git` CLI directly, in one place.

The codebase historically had two parallel patterns (`git_state._git` + inline
`subprocess.run(["git", ...])` in smart_git_ops); devloop routes everything here.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 5  # seconds; hooks must stay snappy and never hang on a dead remote


@dataclass(frozen=True)
class GitResult:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def git(repo_dir: str | Path, *args: str, timeout: int = DEFAULT_TIMEOUT) -> GitResult:
    """Run `git -C <repo_dir> <args...>`. Never raises; rc=-1 on timeout/missing git."""
    return _run(["git", "-C", str(repo_dir), *args], timeout)


def git_global(*args: str, timeout: int = DEFAULT_TIMEOUT) -> GitResult:
    """Run `git <args...>` without -C (for repo-agnostic commands). Never raises."""
    return _run(["git", *args], timeout)


def _run(cmd: list[str], timeout: int) -> GitResult:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return GitResult(r.returncode, r.stdout.strip(), r.stderr.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return GitResult(-1, "", str(e))
