"""Board — the prompt-facing read model over devloop's independently-owned facts.

State sources own facts: workspace discovery, repo/branch state, forge PRs, validation,
review results, and requirement ledgers. Board owns the other half: which facts are relevant
to the current session, how they are summarized, and when each summary is delivered.

Board deliberately stores no copy of those facts. Its only persisted state is a per-session
delivery cursor under ``.devloop/board/sessions/``. Granular cursors keep a branch change from
re-sending unchanged validation/references, and keep concurrent sessions from suppressing one
another's context. This read model is also the seam a later UI can consume without becoming a
second source of truth.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from lib import git_state
from domain.forge import MergeReadiness, pr_label

from . import base, store
from .base import LABEL_NUDGE_CAP, REVIEW_NUDGE_CAP, SESSION_TTL_SEC, TURN_TTL_SEC, Reference

if TYPE_CHECKING:
    from .repo import RepoContext
    from .workspace import WorkspaceContext


class BoardSurface(str, Enum):
    """Delivery surface chosen by Board, never by a fact source."""

    SESSION = "session"
    TURN = "turn"
    EVENT = "event"
    UI_ONLY = "ui_only"


@dataclass(frozen=True)
class _Policy:
    surface: BoardSurface
    max_deliveries: int | None = None
    replay_after_compact: bool = True


_POLICY = {
    "workspace": _Policy(BoardSurface.SESSION),
    "repo.references": _Policy(BoardSurface.SESSION),
    "requirement.current": _Policy(BoardSurface.SESSION),
    "repo.identity": _Policy(BoardSurface.TURN),
    "repo.validation": _Policy(BoardSurface.TURN),
    "repo.pr-blocked": _Policy(
        BoardSurface.EVENT,
        max_deliveries=1,
        replay_after_compact=False,
    ),
    "repo.review": _Policy(
        BoardSurface.EVENT,
        max_deliveries=REVIEW_NUDGE_CAP,
        replay_after_compact=False,
    ),
    "repo.review-label": _Policy(
        BoardSurface.EVENT,
        max_deliveries=LABEL_NUDGE_CAP,
        replay_after_compact=False,
    ),
    "repo.pr-history": _Policy(BoardSurface.UI_ONLY),
}


@dataclass(frozen=True)
class BoardItem:
    """One independently delivered, compact projection of related facts.

    ``identity`` is for event/chore items whose semantic identity is stronger than their
    rendered text (for example a review result or a pending-finding set). State items leave it
    empty and deduplicate on text. ``max_deliveries`` turns an item into a bounded event/nudge;
    those items intentionally survive compaction instead of asking the agent to redo work.
    """

    key: str
    surface: BoardSurface
    text: str
    identity: str = ""
    max_deliveries: int | None = None
    replay_after_compact: bool = True
    data: dict = field(default_factory=dict)

    @property
    def signature(self) -> str:
        raw = self.identity or self.text
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _item(
    key: str,
    text: str,
    *,
    identity: str = "",
    data: dict | None = None,
) -> BoardItem:
    """Apply the one Board policy instead of letting fact sources pick a surface."""
    policy = _POLICY[key]
    return BoardItem(
        key=key,
        surface=policy.surface,
        text=text,
        identity=identity,
        max_deliveries=policy.max_deliveries,
        replay_after_compact=policy.replay_after_compact,
        data=data or {},
    )


@dataclass
class _DeliveryMark:
    signature: str = ""
    count: int = 0
    last_emit_at: float | None = None
    replay_after_compact: bool = True

    @classmethod
    def from_dict(cls, value: dict | None) -> "_DeliveryMark":
        value = value or {}
        return cls(
            signature=str(value.get("signature") or ""),
            count=int(value.get("count", 0) or 0),
            last_emit_at=value.get("last_emit_at"),
            replay_after_compact=bool(value.get("replay_after_compact", True)),
        )


@dataclass
class _DeliveryCursor:
    items: dict[str, _DeliveryMark] = field(default_factory=dict)

    @classmethod
    def load(cls, root: str, session_id: str | None) -> "_DeliveryCursor":
        from .session import session_name

        raw = store.load_segment(root, f"board/sessions/{session_name(session_id)}") or {}
        return cls(items={k: _DeliveryMark.from_dict(v) for k, v in (raw.get("items") or {}).items()})

    def save(self, root: str, session_id: str | None) -> None:
        from .session import session_name

        store.save_segment(root, f"board/sessions/{session_name(session_id)}", asdict(self))

    def due(self, item: BoardItem, now: float) -> bool:
        mark = self.items.get(item.key)
        if mark is None or mark.signature != item.signature:
            return True
        if item.max_deliveries is not None:
            return mark.count < item.max_deliveries
        ttl = SESSION_TTL_SEC if item.surface is BoardSurface.SESSION else TURN_TTL_SEC
        return mark.last_emit_at is None or (now - mark.last_emit_at) >= ttl

    def mark(self, item: BoardItem, now: float) -> None:
        previous = self.items.get(item.key)
        same = previous is not None and previous.signature == item.signature
        self.items[item.key] = _DeliveryMark(
            signature=item.signature,
            count=(previous.count + 1 if same else 1),
            last_emit_at=now,
            replay_after_compact=item.replay_after_compact,
        )


@dataclass
class Board:
    """The relevant board view for one session at one dev root."""

    root: str
    session_id: str | None = None
    workspace: "WorkspaceContext | None" = None
    repo: "RepoContext | None" = None
    stale_repo_note: str | None = None

    @classmethod
    def resolve(cls, cwd: str, session_id: str | None = None) -> "Board | None":
        """Resolve only the workspace/repo relevant to this session.

        At an aggregate-workspace root, the session's active repo supplies the volatile view.
        The lenient read path stays visible with an explicit age warning rather than silently
        dropping context; write workflows continue to use the strict binding elsewhere.
        """
        from domain import repo_layout, workspace as workspace_registry
        from .repo import RepoContext
        from .session import load_active_repo, load_active_repo_lenient
        from .workspace import WorkspaceContext, workspace_for_repo

        ws_root = workspace_registry.find_containing_workspace(cwd)
        git_root = repo_layout.find_git_root(cwd)
        if not ws_root and git_root:
            ws_root = workspace_for_repo(git_root)
        ws = WorkspaceContext.load(ws_root) if ws_root else None

        stale_note = None
        if not git_root and ws_root:
            git_root = load_active_repo(ws_root, session_id)
            if not git_root:
                lenient = load_active_repo_lenient(ws_root, session_id)
                if lenient:
                    git_root, age = lenient
                    stale_note = (
                        f"repo binding is {age / 3600:.1f}h old; state is monitor-fresh, "
                        "but confirm the repo with /enter or cd"
                    )
        repo = (RepoContext.load(git_root) or RepoContext.refresh_all(git_root)) if git_root else None
        root = ws_root or git_root
        return cls(str(root), session_id, ws, repo, stale_note) if root else None

    def items(self) -> list[BoardItem]:
        out: list[BoardItem] = []
        if self.workspace:
            item = _workspace_item(self.workspace)
            if item:
                out.append(item)
        if self.repo:
            out.extend(_repo_items(self.repo, self.stale_repo_note))
        return out

    def render(self, surfaces: Iterable[BoardSurface] | None = None) -> str:
        """Render the full selected view without changing delivery state (tests/UI preview)."""
        selected = set(surfaces) if surfaces is not None else None
        return "\n\n".join(
            item.text
            for item in self.items()
            if selected is None or item.surface in selected
        )

    def emit(self, surfaces: Iterable[BoardSurface] | None = None) -> str | None:
        """Deliver changed/due prompt items; UI-only items never cross this seam."""
        prompt_surfaces = {BoardSurface.SESSION, BoardSurface.TURN, BoardSurface.EVENT}
        selected = (set(surfaces) if surfaces is not None else prompt_surfaces) & prompt_surfaces
        cursor = _DeliveryCursor.load(self.root, self.session_id)
        now = base.now()
        due = [
            item for item in self.items()
            if item.surface in selected and item.text and cursor.due(item, now)
        ]
        if not due:
            return None
        for item in due:
            cursor.mark(item, now)
        cursor.save(self.root, self.session_id)
        return "\n\n".join(item.text for item in due)


def clear_after_compact(root: str, session_id: str | None) -> None:
    """Forget delivered state, while retaining one-shot event/nudge dispositions."""
    cursor = _DeliveryCursor.load(root, session_id)
    changed = False
    for mark in cursor.items.values():
        if mark.replay_after_compact:
            mark.signature = ""
            mark.count = 0
            mark.last_emit_at = None
            changed = True
    if changed:
        cursor.save(root, session_id)


def clear_session(root: str, session_id: str | None) -> None:
    """Remove a normal-ended session's delivery cursor; crashes degrade to harmless cache."""
    from .session import session_name

    try:
        store.segment_file(root, f"board/sessions/{session_name(session_id)}").unlink(missing_ok=True)
    except OSError:
        pass


def render_workspace(workspace: "WorkspaceContext") -> str:
    item = _workspace_item(workspace)
    return item.text if item else ""


def render_repo_session(repo: "RepoContext") -> str:
    return "\n\n".join(
        item.text
        for item in _repo_items(repo, None)
        if item.surface is BoardSurface.SESSION
    )


def render_repo_turn(repo: "RepoContext") -> str:
    """Compatibility preview; real prompt delivery is granular via ``Board.emit``."""
    return " | ".join(
        item.text
        for item in _repo_items(repo, None)
        if item.surface in {BoardSurface.TURN, BoardSurface.EVENT}
    )


def _workspace_item(workspace: "WorkspaceContext") -> BoardItem | None:
    refs = workspace.agents_md.references
    if not refs and not workspace.subprojects:
        return None
    lines = [f"[Workspace: {workspace.workspace_root}]"]
    if refs:
        lines.append("AGENTS.md references (Read when the task touches these topics):")
        lines.extend("  - " + _format_ref(r) for r in refs)
    if workspace.subprojects:
        lines.append("Subprojects:")
        for sub in workspace.subprojects[:12]:
            alias = f" ({', '.join(sub.aliases)})" if sub.aliases else ""
            note = " · ".join(part for part in (sub.language or "", sub.role or "") if part)
            canonical = f" → {sub.canonical}" if sub.canonical else ""
            lines.append(f"  - {sub.name}{alias}: {note}{canonical}")
    return _item("workspace", "\n".join(lines))


def _repo_items(repo: "RepoContext", stale_note: str | None) -> list[BoardItem]:
    items: list[BoardItem] = []
    if repo.agents_md.references:
        lines = ["Repo AGENTS.md references (Read when the task touches these topics):"]
        lines.extend("  - " + _format_ref(r) for r in repo.agents_md.references)
        items.append(_item("repo.references", "\n".join(lines)))

    items.append(_item("repo.identity", _repo_identity(repo, stale_note)))
    items.append(_item("repo.validation", _validation(repo)))

    blocked = _blocked_pr_item(repo)
    if blocked:
        items.append(blocked)

    review = _review_item(repo)
    if review:
        items.append(review)
    if repo.label_pending:
        items.append(_item(
            "repo.review-label",
            f"Review findings: {repo.label_pending} 条待打标 — `ccr:label=`（label-review skill）",
            identity=repo.label_pending_key,
        ))

    # Requirement is already scoped to the current branch and renders nothing for a closed /
    # unrelated delivery. Recent unrelated PRs are intentionally not injected; the current PR
    # is in repo.identity and cross-repo PRs for the current task are in this requirement item.
    from .loopstate import requirement

    requirement_line = requirement.turn_line(repo.repo.repo_dir, repo.branch.local.name or None)
    if requirement_line:
        items.append(_item("requirement.current", requirement_line))
    if repo.prs:
        items.append(_recent_pr_item(repo))
    return items


def _recent_pr_item(repo: "RepoContext") -> BoardItem:
    """Keep the source's PR window visible to Board/UI without spending prompt tokens."""
    labels = [
        f"{pr_label(repo.provider, pr.number)} {pr.state or '?'} ({pr.source_branch or '?'})"
        for pr in repo.prs
    ]
    return _item(
        "repo.pr-history",
        "Recent PR/MR history: " + "; ".join(labels),
        data={
            "provider": repo.provider,
            "pull_requests": [asdict(pr) for pr in repo.prs],
        },
    )


def _repo_identity(repo: "RepoContext", stale_note: str | None) -> str:
    branch = repo.branch
    current = branch.local.name or "?"
    worktree = " (worktree)" if git_state.is_linked_worktree(repo.repo.real_repo_dir) else ""
    extras: list[str] = []
    if branch.local.is_protected():
        extras.append("PROTECTED")
    pr = repo.current_pr()
    if pr and pr.inactive:
        extras.append(
            f"INACTIVE ({pr_label(repo.provider, pr.number)} {pr.state}); cut from origin/{branch.target}"
        )
    elif pr and pr.is_open:
        extras.append(
            f"IN-FLIGHT ({pr_label(repo.provider, pr.number)} open); new work needs a fresh branch "
            "(gcampr --branch)"
        )
    if stale_note:
        extras.append(stale_note)
    warning = f" ⚠️ {'; '.join(extras)}" if extras else ""
    status = _branch_staleness(repo.repo.real_repo_dir or repo.repo.repo_dir, branch)
    dirty = git_state.get_workspace_status(repo.repo.real_repo_dir or repo.repo.repo_dir)
    workspace = (
        f"dirty({dirty.get('modified_count', 0)} modified, {dirty.get('untracked_count', 0)} untracked)"
        if dirty.get("dirty") else "clean"
    )
    return (
        f"[Current repo: {_display_code_dir(repo)} ({repo.repo.language or '?'})] | "
        f"Branch: {current}{worktree} (ahead {status['ahead']}, behind {status['behind']} vs "
        f"{status['base']}{status['asof']}, target={branch.target}) | Workspace: {workspace}{warning}"
    )


def _validation(repo: "RepoContext") -> str:
    validation = repo.validation
    if not validation.components:
        return "Validation: never run"
    parts = [
        f"{cid}: lint={base.fmt_ts(validation.components[cid].last_lint_at)}, "
        f"test={base.fmt_ts(validation.components[cid].last_test_at)}"
        for cid in sorted(validation.components)
    ]
    return "Validation: " + " | ".join(parts)


def _blocked_pr_item(repo: "RepoContext") -> BoardItem | None:
    pr = repo.current_pr()
    if not pr or not pr.is_open or not repo.merge_readiness:
        return None
    try:
        readiness = MergeReadiness(repo.merge_readiness)
    except ValueError:
        return None
    if not readiness.blocks_merge:
        return None
    label = pr_label(repo.provider, pr.number)
    return _item(
        "repo.pr-blocked",
        f"MERGE-BLOCKED: {label} — {_READINESS_BLURB[readiness]}",
        identity=f"{label}:{readiness.value}",
    )


def _review_item(repo: "RepoContext") -> BoardItem | None:
    raw = store.load_segment(
        repo.repo.repo_dir,
        store.branch_segment(repo.branch.local.name or None, "review"),
    ) or {}
    status = _review_status(raw)
    identity = _review_key(raw)
    if not status or not identity:
        return None
    sha = (raw.get("reviewed_sha") or "")[:9]
    if status == "stale":
        text = f"Review: stale on {sha}; see .devloop/review.json"
    elif status == "running":
        text = f"Review: running on {sha}"
    else:
        count, failed = raw.get("count", 0), raw.get("failed", 0)
        parts: list[str] = []
        if count:
            parts.append(f"{count} finding(s)")
        if failed:
            parts.append(f"{failed} file(s) failed")
        if status == "error":
            parts.append("review errored")
        text = (
            f"Review: {', '.join(parts) if parts else 'clean (no findings)'} on {sha}; "
            "see .devloop/review.json"
        )
    return _item(
        "repo.review",
        text,
        identity=identity,
    )


def _review_status(raw: dict) -> str:
    status = raw.get("status")
    if not status or status == "skipped":
        return ""
    if status == "running" and (base.now() - (raw.get("generated_at") or 0)) > base.REVIEW_STALE_SEC:
        return "stale"
    return status


def _review_key(raw: dict) -> str:
    status = _review_status(raw)
    return (
        f"{status}:{raw.get('reviewed_sha') or ''}:{raw.get('count', 0)}:{raw.get('failed', 0)}"
        if status else ""
    )


def _branch_staleness(repo_dir: str, branch) -> dict:
    base_name = branch.base_branch()
    ahead, behind = git_state.get_ahead_behind(repo_dir, base_name) or (0, 0)
    remote = branch.remote_tip(base_name)
    if remote and remote.commit:
        local_mirror = git_state.rev_parse(repo_dir, f"origin/{base_name}")
        if local_mirror and local_mirror != remote.commit:
            asof = f", ⚠ trunk moved since fetch {base.fmt_ts(branch.remotes_fetched_at)}"
        else:
            asof = f", as of {base.fmt_ts(branch.remotes_fetched_at)}"
    else:
        asof = ""
    return {"base": base_name, "ahead": ahead, "behind": behind, "asof": asof}


_READINESS_BLURB = {
    MergeReadiness.CONFLICT: "merge conflict with target",
    MergeReadiness.DISCUSSIONS_UNRESOLVED: "unresolved review discussions",
    MergeReadiness.CI_BLOCKED: "CI not passing",
}


def _display_code_dir(repo: "RepoContext") -> str:
    code_dir = Path(repo.repo.code_dir or repo.repo.real_repo_dir or repo.repo.repo_dir)
    checkout = Path(repo.repo.real_repo_dir or repo.repo.repo_dir or code_dir)
    main = store.state_dir(checkout).parent
    try:
        relative = code_dir.resolve().relative_to(checkout.resolve())
    except (OSError, ValueError):
        return str(code_dir)
    return str(main / relative)


def _format_ref(reference: Reference) -> str:
    title = reference.title or "?"
    description = (reference.hook or "").strip()
    basename = Path(reference.path).name if reference.path else ""
    description_is_path = description and (
        description == basename
        or description == reference.path
        or (description.endswith(".md") and Path(description).name == basename)
    )
    if description and not description_is_path:
        return f"{title} — {description}  ← {basename}"
    return f"{title}  ← {basename}"
