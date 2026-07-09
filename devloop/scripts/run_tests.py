#!/usr/bin/env python3
"""run-test skill 的 CLI 入口：解析 repo，跑 `make test`，通过则盖 test 戳。

test 逻辑见 `lib.lifecycle.checks.test`（与 lifecycle 的 pre_commit / pre_mr gate 是同一段）。
本脚本只做 repo 解析 + 实时输出 + 退出码，并把 `--` 之后的额外参数透传给 make 以手动收窄
范围（按改动收敛 test 选择是后续优化）。

Usage: run_tests.py [--repo R | R] [-- <额外 make/test 参数>]
(R = 路径或 workspace 子项目名；默认 = cwd 的 repo，回退到 workspace 最近活跃 repo。)
Exit: 0 通过或跳过；1 失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import cli  # noqa: E402
from lib.context import record_active_repo  # noqa: E402
from lib.lifecycle import checks  # noqa: E402


def main(argv: list[str]) -> int:
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        extra = argv[i + 1:]
        argv = argv[:i]
    ap = cli.ArgParser(prog="run_tests.py", description="make test; stamp on pass.")
    cli.add_repo_arg(ap)
    ns = ap.parse_args(argv)
    resolved, how = cli.resolve_repo_or_exit(ns, "run_tests")
    repo = resolved.git_root
    if how != "cwd":
        print(f"run_tests: repo = {repo} ({how})")
    record_active_repo(repo)

    res = checks.test(repo, capture=False, extra=extra)   # capture=False：make 实时走到终端
    print(("✓ " if res.ok else "✗ ") + res.summary)
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
