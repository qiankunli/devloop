"""Prompt presentation for typed Board items."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from domain.context import base

from .model import (
    BoardItem,
    ComponentValidationCard,
    PrBlockedCard,
    PrHistoryCard,
    ReferenceCard,
    RepoIdentityCard,
    RepoReferencesCard,
    ReviewCard,
    ReviewLabelCard,
    TextCard,
    ValidationCard,
    WorkspaceCard,
)


def render_prompt(items: Iterable[BoardItem]) -> str:
    return "\n\n".join(text for item in items if (text := render_item(item)))


def render_item(item: BoardItem) -> str:
    payload = item.payload
    if isinstance(payload, WorkspaceCard):
        return _workspace(payload)
    if isinstance(payload, RepoReferencesCard):
        lines = ["Repo AGENTS.md references (Read when the task touches these topics):"]
        lines.extend("  - " + _reference(reference) for reference in payload.references)
        return "\n".join(lines)
    if isinstance(payload, RepoIdentityCard):
        return _repo_identity(payload)
    if isinstance(payload, ValidationCard):
        return _validation(payload.components)
    if isinstance(payload, PrBlockedCard):
        blurbs = {
            "conflict": "merge conflict with target",
            "discussions_unresolved": "unresolved review discussions",
            "ci_blocked": "CI not passing",
        }
        return f"MERGE-BLOCKED: {payload.label} — {blurbs[payload.readiness]}"
    if isinstance(payload, ReviewCard):
        return _review(payload)
    if isinstance(payload, ReviewLabelCard):
        return f"Review findings: {payload.pending} 条待打标 — `ccr:label=`（label-review skill）"
    if isinstance(payload, PrHistoryCard):
        labels = [
            f"{_pr_label(payload.provider, pr.number)} {pr.state or '?'} "
            f"({pr.source_branch or '?'})"
            for pr in payload.pull_requests
        ]
        return "Recent PR/MR history: " + "; ".join(labels)
    if isinstance(payload, TextCard):
        return payload.text
    raise TypeError(f"unsupported Board payload: {type(payload)!r}")


def _workspace(card: WorkspaceCard) -> str:
    lines = [f"[Workspace: {card.root}]"]
    if card.references:
        lines.append("AGENTS.md references (Read when the task touches these topics):")
        lines.extend("  - " + _reference(reference) for reference in card.references)
    if card.subprojects:
        lines.append("Subprojects:")
        for sub in card.subprojects[:12]:
            alias = f" ({', '.join(sub.aliases)})" if sub.aliases else ""
            note = " · ".join(part for part in (sub.language, sub.role) if part)
            canonical = f" → {sub.canonical}" if sub.canonical else ""
            lines.append(f"  - {sub.name}{alias}: {note}{canonical}")
    return "\n".join(lines)


def _reference(reference: ReferenceCard) -> str:
    title = reference.title or "?"
    description = reference.description.strip()
    basename = Path(reference.path).name if reference.path else ""
    description_is_path = description and (
        description == basename
        or description == reference.path
        or (description.endswith(".md") and Path(description).name == basename)
    )
    if description and not description_is_path:
        return f"{title} — {description}  ← {basename}"
    return f"{title}  ← {basename}"


def _repo_identity(card: RepoIdentityCard) -> str:
    worktree = " (worktree)" if card.linked_worktree else ""
    extras: list[str] = []
    if card.protected:
        extras.append("PROTECTED")
    if card.pr_lifecycle == "inactive":
        extras.append(
            f"INACTIVE ({card.pr_label} {card.pr_state}); cut from origin/{card.target_branch}"
        )
    elif card.pr_lifecycle == "in_flight":
        extras.append(
            f"IN-FLIGHT ({card.pr_label} open); new work needs a fresh branch (gcampr --branch)"
        )
    if card.stale_binding_hours is not None:
        extras.append(
            f"repo binding is {card.stale_binding_hours:.1f}h old; state is monitor-fresh, "
            "but confirm the repo with /enter or cd"
        )
    warning = f" ⚠️ {'; '.join(extras)}" if extras else ""
    if card.trunk_moved_since_fetch:
        as_of = f", ⚠ trunk moved since fetch {base.fmt_ts(card.remote_checked_at)}"
    elif card.remote_checked_at:
        as_of = f", as of {base.fmt_ts(card.remote_checked_at)}"
    else:
        as_of = ""
    workspace = (
        f"dirty({card.modified_count} modified, {card.untracked_count} untracked)"
        if card.workspace_dirty else "clean"
    )
    return (
        f"[Current repo: {card.code_dir} ({card.language or '?'})] | "
        f"Branch: {card.branch or '?'}{worktree} (ahead {card.ahead}, behind {card.behind} vs "
        f"{card.base_branch}{as_of}, target={card.target_branch}) | Workspace: {workspace}{warning}"
    )


def _validation(components: tuple[ComponentValidationCard, ...]) -> str:
    if not components:
        return "Validation: never run"
    parts = [
        f"{item.component}: lint={base.fmt_ts(item.lint_at)}, test={base.fmt_ts(item.test_at)}"
        for item in components
    ]
    return "Validation: " + " | ".join(parts)


def _review(card: ReviewCard) -> str:
    sha = card.reviewed_sha[:9]
    if card.status == "stale":
        return f"Review: stale on {sha}; see {card.artifact_path}"
    if card.status == "running":
        return f"Review: running on {sha}"
    parts: list[str] = []
    if card.findings:
        parts.append(f"{card.findings} finding(s)")
    if card.failed_files:
        parts.append(f"{card.failed_files} file(s) failed")
    if card.status == "error":
        parts.append("review errored")
    return (
        f"Review: {', '.join(parts) if parts else 'clean (no findings)'} on {sha}; "
        f"see {card.artifact_path}"
    )


def _pr_label(provider: str, number: int) -> str:
    return f"PR #{number}" if provider == "github" else f"MR !{number}"
