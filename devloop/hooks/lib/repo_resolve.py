"""Resolve which repo a devloop script should operate on — independent of cwd.

The aggregate-workspace loop's single biggest observed tax is that the shell's cwd
snaps back to the workspace root between Bash calls, so every script invocation had
to be hand-prefixed with `cd <repo> && ...` — and a missed prefix died with
"not a git repo". This module gives scripts a cwd-independent answer, resolved from
the state bus instead of the shell:

  1. explicit query — a path, or a subproject name fuzzy-matched against the
     registered workspaces' structured state (`context.json` subprojects);
  2. cwd's enclosing git repo (the classic case, unchanged);
  3. the cwd workspace's last-active repo (`active.json`, stamped by the activity
     writers: CwdChanged / PostToolUse hooks and the smart scripts themselves).

Fuzzy scoring is shared with `resolve_subproject.py` (/enter), so a name means the
same thing everywhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import repo_layout, workspace
from .context import RepoContext, WorkspaceContext
from .context.workspace import load_active_repo, workspace_for_repo


@dataclass(frozen=True)
class ResolvedRepo:
    """The four path identities of one resolved repo, computed ONCE at the resolution
    boundary so consumers stop re-deriving them ad hoc（symlink/canonical/code-dir 语义
    散落在各消费方时，路径不一致问题会反复出现）:

    - `git_root`: 解析入口路径（可能经 symlink，保留调用方视角）
    - `real_git_root`: realpath 后的 canonical 路径（比较 / rebase 用这个）
    - `code_dir`: make/uv 的 workdir（Python 常是 server/、backend/）
    - `workspace_root`: 所属聚合工作区根（单仓库模式为 None）
    - `source`: 解析来源自述（"cwd" / "subproject 'x'" / …），进 PLAN banner
    """
    git_root: str
    real_git_root: str
    code_dir: str
    workspace_root: str | None
    source: str


def _resolved(git_root: str, source: str) -> tuple[ResolvedRepo, str]:
    ctx = RepoContext.load(git_root)
    code = (ctx.repo.code_dir if ctx and ctx.repo.code_dir else None) or repo_layout.find_repo_code_dir(git_root)
    # workspace_for_repo, NOT plain containment: workspaces are symlink farms, so the
    # canonical git_root usually lives outside the workspace tree — containment-only
    # would report workspace_root=None for every symlinked subproject (Mode B 误判)
    ws = workspace_for_repo(git_root)
    r = ResolvedRepo(
        git_root=str(git_root),
        real_git_root=str(Path(git_root).resolve()),
        code_dir=str(code),
        workspace_root=str(ws) if ws else None,
        source=source,
    )
    return r, source


def looks_like_path(s: str) -> bool:
    return "/" in s or s.startswith(".") or s.startswith("~")


def score(query: str, name: str) -> int | None:
    """Lower is better. None = no match."""
    q, n = query.lower(), name.lower()
    if n == q:
        return 0
    if n.startswith(q):
        return 10 + (len(n) - len(q))
    idx = n.find(q)
    if idx >= 0:
        return 100 + idx + (len(n) - len(q))
    i = 0
    for ch in n:
        if i < len(q) and ch == q[i]:
            i += 1
    if i == len(q):
        return 1000 + (len(n) - len(q))
    return None


def best_score(query: str, names: list[str]) -> int | None:
    scores = [sc for name in names if name and (sc := score(query, name)) is not None]
    return min(scores) if scores else None


def match_subprojects(query: str, ws_root: str | Path) -> list[tuple[int, str, str]]:
    """Scored subproject matches in one workspace: (score, name, resolved_abs_path)."""
    ctx = WorkspaceContext.load(ws_root)
    if ctx is None or not ctx.subprojects:
        ctx = WorkspaceContext.refresh(ws_root)
    scored: list[tuple[int, str, str]] = []
    for s in ctx.subprojects:
        if not s.name:
            continue
        sc = best_score(query, [s.name, *s.aliases])
        if sc is None:
            continue
        abs_path = (Path(ws_root) / (s.path or s.name)).resolve()
        if abs_path.is_dir():
            scored.append((sc, s.name, str(abs_path)))
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored


def resolve_repo_dir(query: str | None, cwd: str | Path = ".") -> tuple[ResolvedRepo | None, str]:
    """Resolve (ResolvedRepo, how). On failure: (None, reason-for-the-caller-to-print).

    `how`（== `ResolvedRepo.source`）is a short self-narration ("cwd" / "subproject 'x'" /
    "last-active repo") so scripts can put the resolution in their PLAN banner — a wrong
    guess must be visible. Path identities (real/code_dir/workspace) are computed here
    once; consumers must NOT re-derive them from the raw string.
    """
    cwd = Path(cwd).resolve()

    if query:
        if looks_like_path(query):
            p = Path(os.path.expanduser(os.path.expandvars(query)))
            if not p.is_absolute():
                p = cwd / p
            root = repo_layout.find_git_root(p) if p.is_dir() else None
            if root:
                return _resolved(root, f"path '{query}'")
            return None, f"--repo path is not (in) a git repo: {p}"
        # subproject name: containing workspace first (ordering only breaks ties),
        # then every registered workspace — cwd may be anywhere.
        seen: set[str] = set()
        scored: list[tuple[int, str, str]] = []
        containing = workspace.find_containing_workspace(cwd)
        for w in ([containing] if containing else []) + workspace.load_workspaces():
            wr = str(Path(w).resolve())
            if wr in seen:
                continue
            seen.add(wr)
            scored += match_subprojects(query, wr)
        if not scored:
            # Not a known subproject — maybe a bare relative dirname (`run_fixlint.py devloop`).
            p = cwd / query
            root = repo_layout.find_git_root(p) if p.is_dir() else None
            if root:
                return _resolved(root, f"path '{query}'")
            return None, f"no subproject matches '{query}' in any registered workspace"
        scored.sort(key=lambda t: (t[0], t[1]))
        exact = [x for x in scored if x[0] == 0]
        if len(exact) == 1 or len(scored) == 1:
            path = (exact or scored)[0][2]
            root = repo_layout.find_git_root(path)
            if root:
                return _resolved(root, f"subproject '{(exact or scored)[0][1]}'")
            return None, f"subproject '{query}' resolved to {path}, which is not a git repo"
        cands = ", ".join(f"{n} ({p})" for _, n, p in scored[:4])
        return None, f"'{query}' is ambiguous: {cands} — pass a more specific name or a path"

    root = repo_layout.find_git_root(cwd)
    if root:
        return _resolved(root, "cwd")

    ws_root = workspace.find_containing_workspace(cwd)
    if ws_root:
        active = load_active_repo(ws_root)
        if active:
            return _resolved(active, f"workspace last-active repo '{Path(active).name}'")
        ctx = WorkspaceContext.load(ws_root)
        names = ", ".join(s.name for s in (ctx.subprojects if ctx else []) if s.name)
        known = f"; known subprojects: {names}" if names else ""
        return None, f"cwd is the workspace root and no recent activity recorded — pass --repo <name|path>{known}"

    return None, "not in a git repo — cd into one or pass --repo <name|path>"
