"""Board's typed, delivery-agnostic read model."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum


class BoardItemType(str, Enum):
    WORKSPACE = "workspace"
    REPO_REFERENCES = "repo.references"
    REQUIREMENT_CURRENT = "requirement.current"
    REPO_IDENTITY = "repo.identity"
    REPO_VALIDATION = "repo.validation"
    REPO_PR_BLOCKED = "repo.pr-blocked"
    REPO_REVIEW = "repo.review"
    REPO_REVIEW_LABEL = "repo.review-label"
    REPO_PR_HISTORY = "repo.pr-history"


class BoardItemKind(str, Enum):
    """What an item means; delivery timing is deliberately a separate concern."""

    STATE = "state"
    EVENT = "event"
    DETAIL = "detail"


@dataclass(frozen=True)
class BoardScope:
    workspace_root: str
    repo_root: str | None = None


@dataclass(frozen=True)
class ReferenceCard:
    title: str
    path: str
    description: str = ""


@dataclass(frozen=True)
class SubprojectCard:
    name: str
    aliases: tuple[str, ...] = ()
    language: str = ""
    role: str = ""
    canonical: str = ""


@dataclass(frozen=True)
class WorkspaceCard:
    root: str
    references: tuple[ReferenceCard, ...]
    subprojects: tuple[SubprojectCard, ...]


@dataclass(frozen=True)
class RepoReferencesCard:
    references: tuple[ReferenceCard, ...]


@dataclass(frozen=True)
class RepoIdentityCard:
    code_dir: str
    language: str
    branch: str
    linked_worktree: bool
    ahead: int
    behind: int
    base_branch: str
    remote_checked_at: float | None
    trunk_moved_since_fetch: bool
    target_branch: str
    workspace_dirty: bool
    modified_count: int
    untracked_count: int
    protected: bool = False
    pr_lifecycle: str = ""
    pr_label: str = ""
    pr_state: str = ""
    stale_binding_hours: float | None = None


@dataclass(frozen=True)
class ComponentValidationCard:
    component: str
    lint_at: float | None
    test_at: float | None


@dataclass(frozen=True)
class ValidationCard:
    components: tuple[ComponentValidationCard, ...]


@dataclass(frozen=True)
class PrBlockedCard:
    label: str
    readiness: str


@dataclass(frozen=True)
class ReviewCard:
    status: str
    reviewed_sha: str
    findings: int
    failed_files: int
    artifact_path: str = ".devloop/review.json"


@dataclass(frozen=True)
class ReviewLabelCard:
    pending: int
    pending_set: str


@dataclass(frozen=True)
class PullRequestCard:
    number: int
    state: str
    source_branch: str
    target_branch: str
    title: str
    web_url: str


@dataclass(frozen=True)
class PrHistoryCard:
    provider: str
    pull_requests: tuple[PullRequestCard, ...]


@dataclass(frozen=True)
class TextCard:
    """Temporary bridge for a domain that has not exposed a typed Board projection yet."""

    source: str
    text: str


BoardPayload = (
    WorkspaceCard
    | RepoReferencesCard
    | RepoIdentityCard
    | ValidationCard
    | PrBlockedCard
    | ReviewCard
    | ReviewLabelCard
    | PrHistoryCard
    | TextCard
)


@dataclass(frozen=True)
class BoardItem:
    type: BoardItemType
    kind: BoardItemKind
    scope: BoardScope
    payload: BoardPayload

    @property
    def id(self) -> str:
        """Stable identity inside a Board, including repo scope when applicable."""
        owner = self.scope.repo_root or self.scope.workspace_root
        return f"{owner}:{self.type.value}"

    @property
    def signature(self) -> str:
        """Stable revision of the typed fact projection, independent of presentation."""
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=_json_value)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        """Return the presentation-neutral shape consumed by the future Board UI."""
        return {
            "id": self.id,
            "type": self.type.value,
            "kind": self.kind.value,
            "scope": _json_ready(self.scope),
            "payload": _json_ready(self.payload),
        }


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported Board value: {type(value)!r}")


def _json_ready(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _json_ready(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


@dataclass(frozen=True)
class BoardFocus:
    workspace_root: str
    repo_root: str | None = None


@dataclass(frozen=True)
class BoardView:
    """A relevance-filtered view; session delivery state lives elsewhere."""

    root: str
    focus: BoardFocus | None
    items: tuple[BoardItem, ...]

    def select(self, items: tuple[BoardItem, ...]) -> BoardView:
        return BoardView(root=self.root, focus=self.focus, items=items)

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "focus": _json_ready(self.focus) if self.focus else None,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class Board:
    """Shared structured facts for prompt delivery today and collaborative UI later."""

    root: str
    items: tuple[BoardItem, ...]

    def view(self, focus: BoardFocus | None = None) -> BoardView:
        if focus is None or focus.repo_root is None:
            selected = self.items
        else:
            selected = tuple(
                item for item in self.items
                if item.scope.repo_root is None or item.scope.repo_root == focus.repo_root
            )
        return BoardView(root=self.root, focus=focus, items=selected)
