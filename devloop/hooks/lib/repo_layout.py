"""repo_dir / repo_code_dir / language / AGENTS.md location helpers. Pure stdlib.

Routes the git call through `gitcmd`
(the single git runner) instead of an inline subprocess.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import gitcmd


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
