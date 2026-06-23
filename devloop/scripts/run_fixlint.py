#!/usr/bin/env python3
"""`/lint` 的 CLI 入口：解析 repo，跑 `make fix` + lint target，通过则盖 lint 戳。

实际逻辑（target 选择、warm-cache / CI-entry drift 的处理、盖戳）在 `lib.checks.lint`——
和 lifecycle 的 pre_commit gate 共用同一段，避免漂移。本脚本只做 repo 解析 + 实时输出 +
退出码。只有 `make fix` 能改文件，从不手改代码来满足 linter。

Usage: run_fixlint.py [--repo R | R]   (R = 路径或 workspace 子项目名；
默认 = cwd 的 repo，回退到 workspace 最近活跃 repo)
Exit: 0 通过或干净跳过；1 lint 失败（输出已显示）。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import checks, cli  # noqa: E402
from lib.context import record_active_repo  # noqa: E402


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(prog="run_fixlint.py", description="make fix + lint; stamp on pass.")
    cli.add_repo_arg(ap)
    ns = ap.parse_args(argv)
    resolved, how = cli.resolve_repo_or_exit(ns, "run_fixlint")
    repo = resolved.git_root
    if how != "cwd":
        print(f"run_fixlint: repo = {repo} ({how})")
    record_active_repo(repo)

    res = checks.lint(repo, capture=False)   # capture=False：make 实时走到终端
    print(("✓ " if res.ok else "✗ ") + res.summary)
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
