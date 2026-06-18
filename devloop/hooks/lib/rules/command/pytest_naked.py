"""仓里有 `make test` 目标时拦裸 `pytest`（裸 pytest 常因缺 PYTHONPATH=. 收集 0 项）。

CHANGE 级：要按 env-aware 的 segment 判断（`PYTHONPATH=. pytest` 带 env 前缀 → 放行），
而投影出的 Command 已剥 env，故直接读原始命令串重解析。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from lib import repo_layout
from lib.cmdtree import cmdparse
from lib.context import RepoContext
from lib.core.domain import Change, Finding, Severity, TargetKind
from lib.core.protocol import Rule


def _is_naked_pytest(seg: list[str]) -> bool:
    """seg = 一个 segment 的 tokens（env 未剥）。带 env 前缀则不算裸。"""
    if not seg:
        return False
    if "=" in seg[0] and not seg[0].startswith("-"):
        return False  # 有 env 前缀（如 PYTHONPATH=.）→ 放行
    toks = seg
    if toks[0] == "uv" and "run" in toks[:2]:
        toks = toks[2:]
    base = os.path.basename(toks[0]) if toks else ""
    if base == "pytest":
        return True
    return bool(base.startswith("python") and toks[1:3] == ["-m", "pytest"])


def _has_make_test(code_dir: str) -> bool:
    makefile = Path(code_dir) / "Makefile"
    if not makefile.exists():
        return False
    try:
        return bool(re.search(r"^test(-\w+)?\s*:", makefile.read_text(encoding="utf-8"), re.MULTILINE))
    except OSError:
        return False


class PytestNakedRule(Rule):
    name = "pytest-naked"
    target_kind = TargetKind.CHANGE

    def applies(self, change: Change, ctx) -> bool:
        return bool(change.command)

    def check(self, change: Change, ctx) -> list[Finding]:
        if not any(_is_naked_pytest(seg) for seg in cmdparse.segments(change.command)):
            return []
        git_root = repo_layout.find_git_root(change.cwd)
        if not git_root:
            return []
        rc = RepoContext.load(git_root)
        code_dir = rc.repo.code_dir if rc else git_root
        if not _has_make_test(code_dir):
            return []
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    "⚠️  Bare pytest often fails (PYTHONPATH=. missing → collected 0 items).\n"
                    f"Use the Makefile target:  cd {code_dir} && make test\n"
                    f"Single-case debug:  cd {code_dir} && PYTHONPATH=. .venv/bin/python -m pytest <path> -k <case>"
                ),
                locator=change.command,
            )
        ]
