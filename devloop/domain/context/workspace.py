"""`WorkspaceContext` — aggregate-workspace state at `<workspace_root>/.devloop/context.json`.

Workspace facts (References + Subprojects); no turn-grain content
(branch/dirty/validation are repo-level). Board owns their prompt delivery. The
registry of which dirs are workspaces lives in `domain/workspace.py`.

The workspace `.devloop/` also hosts the session-grain `active/` dir (each
session's bound repo) — that is a different fact-owner grain and lives in
`session.py`: state-bus modules are organized by owner, not by where the file sits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib import parsers
from . import base, store
from .base import (
    WORKSPACE_STALE_SEC,
    AgentsMd,
    Reference,
)


@dataclass
class Subproject:
    name: str = ""
    path: str = ""
    aliases: list[str] = field(default_factory=list)
    language: str | None = None
    role: str | None = None
    canonical: str | None = None   # realpath when it differs from the workspace path (symlink farm)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Subproject":
        d = d or {}
        return cls(
            name=d.get("name", ""),
            path=d.get("path", ""),
            aliases=list(d.get("aliases") or []),
            language=d.get("language"),
            role=d.get("role"),
            canonical=d.get("canonical"),
        )


@dataclass
class WorkspaceContext:
    workspace_root: str = ""
    agents_md: AgentsMd = field(default_factory=AgentsMd)
    subprojects: list[Subproject] = field(default_factory=list)
    parsed_at: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkspaceContext":
        return cls(
            workspace_root=d.get("workspace_root", ""),
            agents_md=AgentsMd.from_dict(d.get("agents_md") or {}),
            subprojects=[Subproject.from_dict(s) for s in (d.get("subprojects") or [])],
            parsed_at=float(d.get("parsed_at", 0) or 0),
        )

    @classmethod
    def load(cls, workspace_root: str | Path) -> "WorkspaceContext | None":
        raw = store.load_raw(workspace_root)
        if raw is None:
            return None
        ctx = cls.from_dict(raw)
        if not ctx.workspace_root:
            ctx.workspace_root = str(Path(workspace_root))
        return ctx

    def save(self) -> None:
        root = self.workspace_root
        if not root:
            return
        self.parsed_at = base.now()
        store.save_raw(root, store.to_dict(self))
        # A workspace root is usually not a git repo; exclude only if it is one.
        if (Path(root) / ".git").exists():
            from lib import git_state
            git_state.ensure_gitignore_excluded(root)

    @classmethod
    def refresh(cls, workspace_root: str | Path) -> "WorkspaceContext":
        root = Path(workspace_root).resolve()
        agents_md = root / "AGENTS.md"
        items, table_rows = [], []
        if agents_md.exists():
            items = parsers.parse_references_section(agents_md)
            table_rows = parsers.parse_subprojects_section(agents_md)
        new = cls(
            workspace_root=str(root),
            agents_md=AgentsMd(
                path=str(agents_md) if agents_md.exists() else None,
                references=[Reference(title=r.get("title", ""), path=r.get("path", ""),
                                      hook=r.get("description")) for r in items],
            ),
            subprojects=_merge_subprojects(root, table_rows),
        )
        new.save()
        return new

    def is_stale(self, ttl: float = WORKSPACE_STALE_SEC) -> bool:
        return base.is_stale(self.parsed_at, ttl)

    # ── Board render preview (delivery itself lives in context.board) ──────────
    def session_text(self) -> str:
        from .board import render_workspace

        return render_workspace(self)


# Direct children that are never subprojects, skipped before the git-repo test. The
# git-repo test alone already drops them; this just avoids stat'ing the obvious ones
# (and documents intent). Hidden dirs (`.devloop`, `.git`, …) are skipped by the dot rule.
_DISCOVERY_SKIP = {"docs", "worktrees", "worktree", "node_modules"}


def discover_subproject_names(root: str | Path) -> list[str]:
    """Filesystem source of truth: workspace direct children that ARE (or symlink to) a
    git repo are subprojects. This — not the AGENTS.md table — decides existence, so
    adding a subproject is ≈ dropping a symlink in. Returns dir names, sorted."""
    root = Path(root)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    out: list[str] = []
    for child in children:
        name = child.name
        if name.startswith(".") or name in _DISCOVERY_SKIP:
            continue
        try:
            if not child.is_dir():               # follows symlinks → symlink-to-dir qualifies
                continue
            if not (child / ".git").exists():     # .git dir (repo) or .git file (worktree/submodule)
                continue
        except OSError:
            continue
        out.append(name)
    return out


def _merge_subprojects(root: Path, table_rows: list[dict]) -> list[Subproject]:
    """Join the AGENTS.md table (optional garnish: aliases/language/role) onto the
    filesystem-discovered set, by name (= dir name). Discovery establishes existence;
    table rows that still resolve to an existing dir but weren't discovered (table-only
    legacy workspaces, or a non-git subdir) are kept too so we converge without a hard cut."""
    table_by_name = {r["name"]: r for r in table_rows if r.get("name")}
    names: list[str] = []
    seen: set[str] = set()
    for name in discover_subproject_names(root):
        names.append(name)
        seen.add(name)
    for row in table_rows:
        nm = row.get("name")
        if nm and nm not in seen and (root / (row.get("path") or nm)).is_dir():
            names.append(nm)
            seen.add(nm)
    return [_build_subproject(root, {**table_by_name.get(nm, {}), "name": nm, "path": nm})
            for nm in names]


def _build_subproject(root: Path, s: dict) -> Subproject:
    sub = Subproject(name=s.get("name", ""), path=s.get("path", ""),
                     aliases=s.get("aliases", []), language=s.get("language"),
                     role=s.get("role") or s.get("note"))
    rel = sub.path or sub.name
    sp_dir = root / rel
    if sp_dir.is_dir():
        # Carry the realpath only when the subproject entry itself is a symlink —
        # compare against the resolved root so symlinked *parents* (e.g. /tmp) don't
        # mark every plain subdir as canonical-divergent.
        canon = sp_dir.resolve()
        if canon != Path(root).resolve() / rel:
            sub.canonical = str(canon)
        # Auto-detect language when the table didn't pin one (table value wins).
        if not sub.language:
            from lib import ecosystem

            from .. import repo_layout
            sub.language = ecosystem.detect_language(repo_layout.find_repo_code_dir(sp_dir))
    return sub


def workspace_for_repo(repo_dir: str | Path) -> str | None:
    """Which registered workspace owns `repo_dir`, if any.

    Plain containment is not enough: workspaces are symlink farms, so the canonical
    repo path (what `git rev-parse --show-toplevel` returns) usually lives OUTSIDE the
    workspace root. Match against each workspace's subproject realpaths too.
    """
    from .. import workspace as registry
    try:
        rd = Path(repo_dir).resolve()
    except OSError:
        return None
    for w in registry.load_workspaces():
        wr = Path(w).resolve()
        try:
            rd.relative_to(wr)
            return str(wr)
        except ValueError:
            pass
        # First entry into a registered workspace may precede any context.json (the
        # init scripts are optional) — build it here or the symlink match below can
        # never succeed and active.json is silently never written. The is_stale gate
        # keeps a workspace with no AGENTS.md from re-parsing on every call.
        ctx = WorkspaceContext.load(wr)
        if ctx is None or (not ctx.subprojects and ctx.is_stale()):
            ctx = WorkspaceContext.refresh(wr)
        for s in (ctx.subprojects if ctx else []):
            sp = wr / (s.path or s.name)
            if not sp.is_dir():
                continue
            try:
                rd.relative_to(sp.resolve())
                return str(wr)
            except ValueError:
                continue
    return None
