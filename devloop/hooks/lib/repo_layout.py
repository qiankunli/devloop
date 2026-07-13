"""repo_dir / repo_code_dir / language / AGENTS.md location helpers. Pure stdlib.

Routes the git call through `gitcmd`
(the single git runner) instead of an inline subprocess.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd


@dataclass(frozen=True)
class CodeUnit:
    """仓库内一个自带工具链、可独立 build/lint/test 的目录（make/uv 的 workdir）。

    一个 git 仓可含多个 unit（`server/` + `cli/`、`packages/*`、`cmd/*`）；「操作落在哪个
    unit」由**操作目标路径**决定，不由 repo 的单值属性决定——这正是单值 `code_dir` 在多代码
    目录仓上选错目录的根因。path+language 在解析边界一次算清、绑成一个值对象随解析结果下传，
    消费方不再各自 `detect_language` 重推（与 `ResolvedRepo` 同动机，低一层）。"""
    path: str
    language: str | None = None


def find_git_root(cwd: str | Path) -> str | None:
    """Find git root by walking up. Returns absolute path or None if not in a git repo."""
    r = gitcmd.git(cwd, "rev-parse", "--show-toplevel", timeout=3)
    return r.out if r.ok and r.out else None


def is_git_repo(path: str | Path) -> bool:
    return find_git_root(path) is not None


def find_repo_code_dir(repo_dir: str | Path) -> str:
    """Find the actual code directory inside a repo.

    Python projects often use server/ or backend/ as repo_code_dir.
    Go and TS projects usually use repo_dir itself.
    """
    repo_dir = Path(repo_dir)
    for sub in ("server", "backend"):
        candidate = repo_dir / sub
        if candidate.is_dir() and _has_code_markers(candidate):
            return str(candidate)
    return str(repo_dir)


def default_code_unit(git_root: str | Path) -> CodeUnit:
    """repo 级默认 unit：没有更具体的操作目标路径时用（如按名字 /enter 一个仓、cwd 就是仓根）。
    探测规则同 `find_repo_code_dir`（`server/` > `backend/` > repo 根）。"""
    code_dir = find_repo_code_dir(git_root)
    return CodeUnit(code_dir, detect_language(code_dir))


def enclosing_code_unit(target: str | Path, git_root: str | Path) -> CodeUnit:
    """操作目标 `target`（文件或目录）落在哪个 code unit：从 target 向上找**最近**的带工具链清单
    的目录，边界止于 `git_root`（不越出仓）。

    当 target 就是（或在）仓根、其间没有更具体的 unit 时回落 `default_code_unit`——所以
    `run_tests.py doctor` 仍走默认 `server/`，而 `run_tests.py doctor/cli` 精确命中 `cli/`。
    仓根自身的 orchestration Makefile 不抢默认：target==git_root 直接走 default（探测顺序说了算）。"""
    root = Path(git_root).resolve()
    p = Path(target).resolve()
    cur = p if p.is_dir() else p.parent
    # 只在 git_root 严格子目录里向上走；命中 marker 即为该 unit。target 在仓根或仓外 →
    # 循环不进入，落到 default（仓根的探测优先级说了算，别被根级编排 Makefile 抢走）。
    while cur != root and root in cur.parents:
        if _has_code_markers(cur):
            return CodeUnit(str(cur), detect_language(str(cur)))
        cur = cur.parent
    return default_code_unit(git_root)


def _has_code_markers(path: Path) -> bool:
    markers = ("pyproject.toml", "go.mod", "package.json", "Makefile", "setup.py", "requirements.txt")
    return any((path / m).exists() for m in markers)


def detect_language(repo_code_dir: str | Path) -> str | None:
    """Detect primary language of repo_code_dir. Returns 'python' / 'go' / 'typescript' / 'javascript' / None."""
    p = Path(repo_code_dir)
    if (p / "pyproject.toml").exists() or (p / "setup.py").exists() or (p / "requirements.txt").exists():
        return "python"
    if (p / "go.mod").exists():
        return "go"
    if (p / "package.json").exists():
        try:
            content = (p / "package.json").read_text(encoding="utf-8")
            if "typescript" in content.lower() or "@types/" in content:
                return "typescript"
        except OSError:
            pass
        return "javascript"
    return None


def find_agents_md(repo_dir: str | Path, repo_code_dir: str | Path | None = None) -> str | None:
    """Locate AGENTS.md. Prefer repo_code_dir, fallback to repo_dir."""
    candidates = []
    if repo_code_dir:
        candidates.append(Path(repo_code_dir) / "AGENTS.md")
    candidates.append(Path(repo_dir) / "AGENTS.md")
    for c in candidates:
        if c.exists():
            return str(c.resolve())
    return None


def expand_user_path(path: str) -> str:
    """Expand ~ and env vars in a config path."""
    return os.path.expanduser(os.path.expandvars(path))
