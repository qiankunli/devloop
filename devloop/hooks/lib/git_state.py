"""Git state queries — branch / protected / ahead-behind / target / worktree.

Every git call routes through `gitcmd` (the single
runner) instead of an inline subprocess. Functions never raise (gitcmd is
failure-safe), so hooks on the hot path stay safe.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import gitcmd

PROTECTED_BRANCH_PATTERNS = (
    re.compile(r"^main$"),
    re.compile(r"^master$"),
    re.compile(r"^release$"),
    re.compile(r"^release.*"),
    re.compile(r".*release$"),
)


def get_current_branch(repo_dir: str | Path) -> str | None:
    r = gitcmd.git(repo_dir, "branch", "--show-current")
    return r.out if r.ok and r.out else None


def is_protected_branch(branch: str | None) -> bool:
    if not branch:
        return False
    return any(p.match(branch) for p in PROTECTED_BRANCH_PATTERNS)


def get_ahead_behind(repo_dir: str | Path, target: str = "release") -> tuple[int, int] | None:
    """(ahead, behind) relative to origin/<target>. None if target/count unavailable."""
    r = gitcmd.git(repo_dir, "rev-list", "--count", f"origin/{target}..HEAD")
    if not r.ok:
        return None
    try:
        ahead = int(r.out)
    except ValueError:
        return None
    r = gitcmd.git(repo_dir, "rev-list", "--count", f"HEAD..origin/{target}")
    if not r.ok:
        return None
    try:
        behind = int(r.out)
    except ValueError:
        return None
    return ahead, behind


def get_workspace_status(repo_dir: str | Path) -> dict:
    """dict with dirty / modified_count / untracked_count."""
    r = gitcmd.git(repo_dir, "status", "--porcelain")
    if not r.ok:
        return {"dirty": False, "modified_count": 0, "untracked_count": 0}
    lines = [ln for ln in r.out.split("\n") if ln.strip()]
    modified = sum(1 for ln in lines if not ln.startswith("??"))
    untracked = sum(1 for ln in lines if ln.startswith("??"))
    return {"dirty": bool(lines), "modified_count": modified, "untracked_count": untracked}


def target_exists(repo_dir: str | Path, target: str = "release") -> bool:
    return gitcmd.git(repo_dir, "rev-parse", "--verify", f"origin/{target}").ok


def get_default_target(repo_dir: str | Path) -> str:
    """fast impl: derive default MR target from the *local* origin/HEAD cache.

    Pure-local, offline-safe, zero network — safe on hot paths. Tradeoff: the
    local `refs/remotes/origin/HEAD` is NOT refreshed by `git fetch`, so it can
    lag a remote default-branch change. `refresh_remote_head` (normal impl) keeps
    it fresh at a low-freq boundary (SessionStart). See plan §5.2.
    """
    r = gitcmd.git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
    if r.ok and r.out.startswith("refs/remotes/origin/"):
        return r.out.split("/", 3)[-1]
    return "release"


def refresh_remote_head(repo_dir: str | Path, timeout: int = 5) -> bool:
    """normal impl: refresh local origin/HEAD from the remote default branch.

    One network round-trip. `git fetch` never touches origin/HEAD, so without
    this a remote default-branch change leaves every derived target stale
    forever. Call only at low-freq boundaries (SessionStart). Best-effort.
    """
    return gitcmd.git(repo_dir, "remote", "set-head", "origin", "--auto", timeout=timeout).ok


def get_worktree_metadata(repo_dir: str | Path) -> tuple[bool, str, str | None]:
    """`(is_linked, common_dir, main_branch)`. One walk because
    `_build_branch_section` needs all three together."""
    r1 = gitcmd.git(repo_dir, "rev-parse", "--git-dir")
    r2 = gitcmd.git(repo_dir, "rev-parse", "--git-common-dir")
    if not r1.ok or not r2.ok or not r1.out or not r2.out:
        return False, "", None
    base = Path(repo_dir)
    try:
        gd = (base / r1.out).resolve()
        cd = (base / r2.out).resolve()
    except OSError:
        return False, "", None
    if gd == cd:
        return False, "", None  # main checkout
    common_dir = str(cd)
    main_branch: str | None = None
    r = gitcmd.git(repo_dir, "worktree", "list", "--porcelain")
    if r.ok and r.out:
        first_block = r.out.split("\n\n", 1)[0]
        for line in first_block.splitlines():
            if line.startswith("branch "):
                ref = line[len("branch "):].strip()
                if ref.startswith("refs/heads/"):
                    main_branch = ref[len("refs/heads/"):]
                break
    return True, common_dir, main_branch


def is_linked_worktree(repo_dir: str | Path) -> bool:
    """True iff a linked worktree (git-dir != git-common-dir). False on error
    → callers treat as main checkout (safe default)."""
    r1 = gitcmd.git(repo_dir, "rev-parse", "--git-dir")
    r2 = gitcmd.git(repo_dir, "rev-parse", "--git-common-dir")
    if not r1.ok or not r2.ok or not r1.out or not r2.out:
        return False
    try:
        return (Path(repo_dir) / r1.out).resolve() != (Path(repo_dir) / r2.out).resolve()
    except OSError:
        return False


def ensure_gitignore_excluded(repo_dir: str | Path, pattern: str = "/.devloop/") -> None:
    """Append `pattern` to git's per-repo `info/exclude` idempotently.

    Local-only (doesn't touch the committed .gitignore). Path resolved via
    `git rev-parse --git-path info/exclude` so linked worktrees (where `.git`
    is a gitlink file) work — hard-coding `<repo>/.git/info` silently failed there.
    """
    r = gitcmd.git(repo_dir, "rev-parse", "--git-path", "info/exclude")
    if not r.ok or not r.out:
        return
    exclude_path = Path(repo_dir) / r.out
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    existing = ""
    if exclude_path.exists():
        try:
            existing = exclude_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    for line in existing.splitlines():
        if line.strip() == pattern.strip():
            return
    sep = "" if existing.endswith("\n") or not existing else "\n"
    try:
        exclude_path.write_text(f"{existing}{sep}{pattern}\n", encoding="utf-8")
    except OSError:
        pass  # best-effort
