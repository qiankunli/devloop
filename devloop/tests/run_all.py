#!/usr/bin/env python3
"""devloop 全量测试入口（stdlib only，无需 pytest）：`python3 devloop/tests/run_all.py`。

逐模块跑所有 test_*.py（test_session_lock.py 用 pytest fixture，只归 pytest 跑）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testkit import run_all  # noqa: E402  (bootstrap first)

MODULES = ["test_cmdparse", "test_forge", "test_git_ops", "test_rebase", "test_notify", "test_board", "test_context", "test_guards", "test_lifecycle", "test_review"]


def main() -> int:
    total, failed = 0, []
    for name in MODULES:
        n, bad = run_all(vars(importlib.import_module(name)), label=name)
        total += n
        failed += bad
    print("TOTAL:", "FAIL " + ", ".join(failed) if failed else f"ALL PASS ({total} tests)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
