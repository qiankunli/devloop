"""Resolve which repo a devloop script should operate on — independent of cwd.

The aggregate-workspace loop's single biggest observed tax is that the shell's cwd
snaps back to the workspace root between Bash calls, so every script invocation had
to be hand-prefixed with `cd <repo> && ...` — and a missed prefix died with
"not a git repo". This module gives scripts a cwd-independent answer, resolved from
the state bus instead of the shell:

  1. explicit query — a path, or a subproject name fuzzy-matched against the
     registered workspaces' structured state (`context.json` subprojects);
  2. cwd's enclosing git repo (the classic case, unchanged);
  3. THIS session's bound repo in the cwd workspace (`.devloop/active/<sid>.json`,
     one file per session, stamped by the activity writers: CwdChanged / PostToolUse
     hooks and the smart scripts themselves; scripts self-identify via
     CLAUDE_CODE_SESSION_ID). Concurrent sessions on different repos can't poison
     each other's fallback; a session with no binding of its own must say --repo —
     other sessions' bindings are only ever surfaced as a hint in that error.

Fuzzy scoring is shared with `resolve_subproject.py` (/enter), so a name means the
same thing everywhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd, repo_layout, workspace
from .context import RepoContext, WorkspaceContext
from .context.session import active_repo_candidates, load_active_repo
from .context.workspace import workspace_for_repo


@dataclass(frozen=True)
class ResolvedRepo:
    """The identities of one resolved repo, computed ONCE at the resolution boundary so
    consumers stop re-deriving them ad hoc（symlink/canonical/code-unit 语义散落在各消费方
    时，路径不一致问题会反复出现）:

    - `git_root`: 解析入口路径（可能经 symlink，保留调用方视角）
    - `real_git_root`: realpath 后的 canonical 路径（比较 / rebase 用这个）
    - `target_path`: 解析入口的操作目标路径——显式路径 / cwd 落在仓内时有值，按名字 / last-active
      兜底时为 None。它是「解析怎么找到这个 repo」的事实，喂给 `select_units` 当 explicit 信号；
      **不是** unit 决策——选哪些 unit 由 `select_units` 按本次改动定（见 `WorkSet`），不再挂在
      解析结果上当单值属性（那正是多代码目录仓选错 unit 的根因）。
    - `workspace_root`: 所属聚合工作区根（单仓库模式为 None）
    - `source`: 解析来源自述（"cwd" / "subproject 'x'" / …），进 PLAN banner
    """
    git_root: str
    real_git_root: str
    target_path: str | None
    workspace_root: str | None
    source: str


def default_unit(git_root: str | Path, ctx: RepoContext | None = None) -> repo_layout.CodeUnit:
    """repo 级**默认** unit：持久化 `code_dir` 缓存优先（= 探测结果的缓存），否则现探
    （`server/` > `backend/` > repo 根）。没有更具体操作目标路径时用——解析边界的默认分支、
    lifecycle gate 的回落、按名字 `/enter` 一个仓，都收敛到这一个入口（单一事实源）。"""
    ctx = ctx if ctx is not None else RepoContext.load(git_root)
    cached = ctx.repo.code_dir if ctx and ctx.repo.code_dir else None
    if cached:
        return repo_layout.CodeUnit(cached, repo_layout.detect_language(cached))
    return repo_layout.default_code_unit(git_root)


@dataclass(frozen=True)
class WorkSet:
    """本轮要处理的 code unit 工作集——由「本次改动」而非「解析来源」决定，消费方（lint / test /
    review hook、gate、注入）对 `units` 逐个 fan-out。**中立**对象：只答「哪些 unit」，不含
    「对它们跑什么 check」——那是消费方各自的事，别塞进来（塞了就从「选范围」滑向「验证专属」）。

    - `units`:  0..N 个 CodeUnit；多代码目录仓一次改动可命中多个，并行处理
    - `how`:    'explicit' / 'dirty' / 'repo-wide'——这批 unit 怎么来的（选择透明，选错一眼可见）
    - `reason`: 面向人的一句自述，进命令输出 / PLAN
    """
    units: tuple[repo_layout.CodeUnit, ...]
    how: str
    reason: str


def _changed_paths(git_root: str | Path) -> list[str]:
    """working tree 里有改动的文件（相对仓根的路径）：tracked 改动（staged + unstaged vs HEAD）
    + untracked。**刻意不用 `status --porcelain`**——`gitcmd` 对输出整体 `strip()`，会吃掉
    porcelain 首行的前导状态空格（` M path`）导致列错位；这两条命令输出纯路径、strip 无害。"""
    paths: list[str] = []
    d = gitcmd.git(git_root, "diff", "--name-only", "HEAD")          # tracked: staged+unstaged
    if d.ok and d.out:
        paths += d.out.splitlines()
    o = gitcmd.git(git_root, "ls-files", "--others", "--exclude-standard")   # untracked
    if o.ok and o.out:
        paths += o.out.splitlines()
    return [p.strip() for p in paths if p.strip()]


def _dirty_units(git_root: str | Path) -> list[repo_layout.CodeUnit]:
    """working tree 里有改动的文件各自归属的 code unit，去重。这是「变更决定验证目标」的核心：
    改了 cli/** 就只投影出 cli，不受解析来源是否带路径影响。"""
    seen: dict[str, repo_layout.CodeUnit] = {}
    for p in _changed_paths(git_root):
        u = repo_layout.enclosing_code_unit(Path(git_root) / p, git_root)
        seen.setdefault(u.path, u)
    return list(seen.values())


def select_units(git_root: str | Path, *, explicit: str | Path | None = None) -> WorkSet:
    """本轮 WorkSet：显式目标 > dirty 改动投影 > repo-wide 全量。任何一级都**不静默回默认 server**。

    - `explicit`（显式 --unit / 路径 / cwd 落在仓内某具体子目录）→ 归属那个 unit。仅当它指向仓根
      **严格子路径**时才算显式；`explicit == 仓根`（cwd 恰停在仓根）不算——否则又会 enclosing 回落
      到 default(server)，正是要消除的那个 bug。
    - 无显式目标 → 看 working tree dirty 文件落在哪些 unit → 全部命中（多 unit 并行）。
    - dirty 为空（clean tree）→ repo-wide：枚举全部 unit。
    """
    root = Path(git_root).resolve()
    if explicit is not None:
        ep = Path(explicit).resolve()
        if ep != root and root in ep.parents:
            u = repo_layout.enclosing_code_unit(ep, git_root)
            return WorkSet((u,), "explicit", f"explicit target {ep.name} → unit {Path(u.path).name}")
        # ep == 仓根：没有具体目标，落到 dirty（不走 enclosing → default → server）
    dirty = _dirty_units(git_root)
    if dirty:
        names = ", ".join(Path(u.path).name for u in dirty)
        return WorkSet(tuple(dirty), "dirty", f"changed files under: {names}")
    allu = repo_layout.discover_code_units(git_root)
    names = ", ".join(Path(u.path).name for u in allu)
    return WorkSet(tuple(allu), "repo-wide", f"clean tree, all units: {names}")


def _resolved(git_root: str, source: str, target_path: str | Path | None = None) -> tuple[ResolvedRepo, str]:
    """把解析结果收成 ResolvedRepo。`target_path` 是解析入口的操作目标（显式路径 / cwd），原样
    带上供 `select_units` 当 explicit 信号——选哪些 unit 不在这里算（那由本次改动定，见 `select_units`）。"""
    # workspace_for_repo, NOT plain containment: workspaces are symlink farms, so the
    # canonical git_root usually lives outside the workspace tree — containment-only
    # would report workspace_root=None for every symlinked subproject (Mode B 误判)
    ws = workspace_for_repo(git_root)
    r = ResolvedRepo(
        git_root=str(git_root),
        real_git_root=str(Path(git_root).resolve()),
        target_path=str(target_path) if target_path is not None else None,
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
                return _resolved(root, f"path '{query}'", target_path=p)
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
                return _resolved(root, f"path '{query}'", target_path=p)
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
        # cwd is the operation target: being in server/ vs cli/ picks that unit.
        return _resolved(root, "cwd", target_path=cwd)

    ws_root = workspace.find_containing_workspace(cwd)
    if ws_root:
        active = load_active_repo(ws_root)
        if active:
            return _resolved(active, f"session last-active repo '{Path(active).name}'")
        cands = active_repo_candidates(ws_root)
        if cands:
            names = ", ".join(Path(c).name for c in cands)
            return None, (
                "this session has no repo activity yet "
                f"(other sessions are on: {names}) — pass --repo <name|path>"
            )
        ctx = WorkspaceContext.load(ws_root)
        names = ", ".join(s.name for s in (ctx.subprojects if ctx else []) if s.name)
        known = f"; known subprojects: {names}" if names else ""
        return None, f"cwd is the workspace root and no recent activity recorded — pass --repo <name|path>{known}"

    return None, "not in a git repo — cd into one or pass --repo <name|path>"
