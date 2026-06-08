#!/usr/bin/env python3
"""Run the repo's test target in its code dir; stamp test on success.

0.1 runs `make test` (the repo's canonical entry — sets PYTHONPATH etc.). Convergent
selection (only tests touched by recent changes) is a future refinement; for now pass
extra args through to narrow scope manually.

Usage: run_tests.py [repo] [-- <extra make/test args>]
(`repo` = a path or a workspace subproject name; default = cwd's repo, falling back
to the workspace's last-active repo.)
Exit: 0 passed or skipped; 1 failed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import repo_resolve  # noqa: E402
from lib.context import RepoContext, record_active_repo  # noqa: E402


def has_target(code_dir: str, name: str) -> bool:
    mk = Path(code_dir) / "Makefile"
    if not mk.exists():
        return False
    try:
        return bool(re.search(rf"^{re.escape(name)}(-\w+)?\s*:", mk.read_text(encoding="utf-8"), re.MULTILINE))
    except OSError:
        return False


def main(argv: list[str]) -> int:
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        extra = argv[i + 1:]
        argv = argv[:i]
    resolved, how = repo_resolve.resolve_repo_dir(argv[0] if argv else None)
    if not resolved:
        print(f"run_tests: {how}", file=sys.stderr)
        return 1
    repo, code_dir = resolved.git_root, resolved.code_dir  # path identities live in ResolvedRepo
    if how != "cwd":
        print(f"run_tests: repo = {repo} ({how})")
    record_active_repo(repo)
    ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)

    if not has_target(code_dir, "test"):
        print(f"run_tests: no `make test` target in {code_dir} — run the repo's tests manually, "
              "then `mark_validation.py test` to stamp.")
        return 0

    print(f"--- make test (cwd={code_dir}) {' '.join(extra)} ---")
    rc = subprocess.run(["make", "test", *extra], cwd=code_dir).returncode
    if rc == 0:
        ctx.mark_test_passed()
        print("✓ tests passed — stamped .devloop validation.")
        return 0
    print("✗ tests failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
