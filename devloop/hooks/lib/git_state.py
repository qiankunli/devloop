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


def get_ahead_behind(repo_dir: str | Path, target: str = "main") -> tuple[int, int] | None:
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


def target_exists(repo_dir: str | Path, target: str = "main") -> bool:
    return gitcmd.git(repo_dir, "rev-parse", "--verify", f"origin/{target}").ok


def local_default_target(repo_dir: str | Path) -> str:
    """Read the default MR target from the *local* origin/HEAD cache — the offline FALLBACK.

    Pure-local, zero network, safe on hot paths. NOT the authority: `git fetch` never refreshes
    `refs/remotes/origin/HEAD`, so this can lag. The authority is the forge value cached in
    `RepoMeta.default_branch` (resolved on a TTL boundary) — callers prefer that and only fall
    here when it's empty / unavailable. No "release" bias: when origin/HEAD is absent, fall back
    to whichever of main/master exists, else main.
    """
    r = gitcmd.git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
    if r.ok and r.out.startswith("refs/remotes/origin/"):
        return r.out.split("/", 3)[-1]
    for b in ("main", "master"):
        if target_exists(repo_dir, b):
            return b
    return "main"


def refresh_remote_head(repo_dir: str | Path, timeout: int = 5) -> bool:
    """Refresh local origin/HEAD from the remote default branch via one network round-trip
    (`git fetch` never touches origin/HEAD). Best-effort. Used as the no-token fallback when
    the forge can't supply the default branch (see context.repo._resolve_default_branch)."""
    return gitcmd.git(repo_dir, "remote", "set-head", "origin", "--auto", timeout=timeout).ok


def set_local_default_head(repo_dir: str | Path, branch: str) -> bool:
    """Point local `refs/remotes/origin/HEAD` at `branch` — purely local, no network.

    Keeps the git-side cache consistent with an authoritative value resolved elsewhere (the
    forge), so every `local_default_target` caller (gcampr, run_review, …) reads the same answer
    without each re-querying. Best-effort.
    """
    if not branch:
        return False
    return gitcmd.git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD",
                      f"refs/remotes/origin/{branch}").ok


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


# ── live identity + ancestry (gate truth / PR selection read these) ───────────
# These are the cheap, always-live facts a write-gate must derive at decision time
# instead of trusting a cached segment (see docs/branch-state.md §gate truth).
def get_head_sha(repo_dir: str | Path) -> str:
    """Current HEAD sha (full). Empty on error. Works on detached HEAD (unlike a
    branch-name read), which is exactly why gates resolve identity through git, not a lib."""
    r = gitcmd.git(repo_dir, "rev-parse", "HEAD")
    return r.out if r.ok else ""


def rev_parse(repo_dir: str | Path, ref: str) -> str:
    """Resolve a ref to a sha (verified). Empty when the ref is absent/unresolvable —
    e.g. a not-yet-fetched `origin/<branch>`."""
    r = gitcmd.git(repo_dir, "rev-parse", "--verify", "--quiet", ref)
    return r.out if r.ok else ""


def is_ancestor(repo_dir: str | Path, ancestor: str | None, descendant: str | None) -> bool:
    """True iff `ancestor` is reachable from `descendant` (`merge-base --is-ancestor`).
    Empty/error → False: callers treat 'unknown' as 'not reachable', which is the safe
    default for PR selection (a dead-ref PR whose sha we can't reach is not the branch's PR)."""
    if not ancestor or not descendant:
        return False
    if ancestor == descendant:
        return True
    return gitcmd.git(repo_dir, "merge-base", "--is-ancestor", ancestor, descendant).rc == 0


# ── remote relationship (read-freshness) ──────────────────────────────────────
def get_upstream_ahead_behind(repo_dir: str | Path) -> tuple[int, int] | None:
    """(ahead, behind) of HEAD vs its OWN upstream (`origin/<current>`), or None when the
    branch has no upstream (fresh feature branch) or it can't be resolved. This is the
    "my branch moved on the server (pushed from elsewhere)" signal — distinct from
    behind-trunk. Bounded to local refs, no network (the upstream ref is whatever the last
    fetch left)."""
    r = gitcmd.git(repo_dir, "rev-list", "--count", "--left-right", "@{upstream}...HEAD")
    if not r.ok or "\t" not in r.out:
        return None
    behind_s, ahead_s = r.out.split("\t", 1)
    try:
        return int(ahead_s), int(behind_s)
    except ValueError:
        return None


def ls_remote_tips(repo_dir: str | Path, *branches: str, timeout: int = 5) -> dict[str, str]:
    """`{branch: sha}` for `branches` on origin via `git ls-remote` — the TRUE remote tip,
    one network round-trip, NO object fetch. The monitor's cheap way to learn that trunk
    moved (a colleague pushed) without pulling history. Empty dict offline/on error."""
    if not branches:
        return {}
    r = gitcmd.git(repo_dir, "ls-remote", "origin", *branches, timeout=timeout)
    if not r.ok:
        return {}
    tips: dict[str, str] = {}
    for line in r.out.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if ref.startswith("refs/heads/"):
            tips[ref[len("refs/heads/"):]] = sha
    return tips


def list_worktrees(repo_dir: str | Path) -> list[tuple[str, str, str | None]]:
    """Every worktree of the repo as `(path, head_sha, branch|None)` via
    `git worktree list --porcelain`. Empty on error."""
    r = gitcmd.git(repo_dir, "worktree", "list", "--porcelain")
    if not r.ok or not r.out:
        return []
    out: list[tuple[str, str, str | None]] = []
    path, sha, branch = "", "", None
    for line in r.out.split("\n"):
        if line.startswith("worktree "):
            path, sha, branch = line[len("worktree "):].strip(), "", None
        elif line.startswith("HEAD "):
            sha = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif not line.strip() and path:
            out.append((path, sha, branch))
            path, sha, branch = "", "", None
    if path:
        out.append((path, sha, branch))
    return out


def fetch(repo_dir: str | Path, *refs: str, timeout: int = 8) -> bool:
    """Bounded `git fetch origin [refs...]` for low-freq, intentional boundaries (/enter).
    Refreshes local remote-tracking refs so behind/ahead become REAL rather than
    relative-to-a-stale-mirror. Best-effort (offline → False)."""
    return gitcmd.git(repo_dir, "fetch", "origin", *refs, "--quiet", timeout=timeout).ok
