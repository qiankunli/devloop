#!/usr/bin/env python3
"""fix-lint skill 的 CLI 入口：解析 repo，跑 `make fix` + lint target，通过则盖 lint 戳。

lint 逻辑见 `lib.lifecycle.checks.lint`（与 lifecycle 的 pre_commit gate 是同一段）。本脚本
只做 repo 解析 + 实时输出 + 退出码。只有 `make fix` 能改文件，从不手改代码来满足 linter。

Usage: run_fixlint.py [--repo R | R]   (R = 路径或 workspace 子项目名；
默认 = cwd 的 repo，回退到 workspace 最近活跃 repo)
Exit: 0 通过或干净跳过；1 lint 失败（输出已显示）。
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
    ap = cli.ArgParser(prog="run_fixlint.py", description="make fix + lint; stamp on pass.")
    cli.add_repo_arg(ap)
    ns = ap.parse_args(argv)
    resolved, how = cli.resolve_repo_or_exit(ns, "run_fixlint")
    repo = resolved.git_root
    unit = resolved.code_unit
    if how != "cwd":
        print(f"run_fixlint: repo = {repo} ({how})")
    if unit.path != repo:   # 多代码目录仓：点明落在哪个 unit
        print(f"run_fixlint: code unit = {unit.path} ({unit.language or '?'})")
    record_active_repo(repo)

    # 用解析到的 unit（按操作目标选出），不让 checks 从 git_root 重探默认 unit 盖掉它。
    res = checks.lint(repo, capture=False, unit=unit)   # capture=False：make 实时走到终端
    print(("✓ " if res.ok else "✗ ") + res.summary)
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
