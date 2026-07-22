"""Application seam shared by Board-related hooks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .delivery import DeliveryChannel, DeliveryPolicy, PromptDelivery, PromptTrigger
from .model import Board, BoardView
from .projection import project_view

if TYPE_CHECKING:
    from domain.context.repo import RepoContext
    from domain.context.workspace import WorkspaceContext


@dataclass
class BoardRuntime:
    """Resolve facts once, then expose the small set of hook lifecycle operations."""

    root: str
    session_id: str | None
    board: Board
    view: BoardView
    repo: RepoContext | None = None

    @classmethod
    def from_facts(
        cls,
        root: str,
        session_id: str | None,
        workspace: WorkspaceContext | None = None,
        repo: RepoContext | None = None,
        stale_binding_hours: float | None = None,
    ) -> BoardRuntime:
        board, view = project_view(root, workspace, repo, stale_binding_hours)
        return cls(root, session_id, board, view, repo)

    @classmethod
    def resolve(cls, cwd: str, session_id: str | None = None) -> BoardRuntime | None:
        """Resolve the workspace/repo relevant to this session and load its fact views."""
        from domain import repo_layout, workspace as workspace_registry
        from domain.context.repo import RepoContext
        from domain.context.session import load_active_repo, load_active_repo_lenient
        from domain.context.workspace import WorkspaceContext, workspace_for_repo

        ws_root = workspace_registry.find_containing_workspace(cwd)
        git_root = repo_layout.find_git_root(cwd)
        if not ws_root and git_root:
            ws_root = workspace_for_repo(git_root)
        workspace = WorkspaceContext.load(ws_root) if ws_root else None

        stale_binding_hours = None
        if not git_root and ws_root:
            git_root = load_active_repo(ws_root, session_id)
            if not git_root:
                lenient = load_active_repo_lenient(ws_root, session_id)
                if lenient:
                    git_root, age = lenient
                    stale_binding_hours = age / 3600
        repo = (RepoContext.load(git_root) or RepoContext.refresh_all(git_root)) if git_root else None
        root = ws_root or git_root
        return (
            cls.from_facts(str(root), session_id, workspace, repo, stale_binding_hours)
            if root else None
        )

    def deliver_prompt(
        self,
        trigger: PromptTrigger = PromptTrigger.USER_PROMPT,
    ) -> str | None:
        return PromptDelivery(self.root, self.session_id).deliver(self.view, trigger)

    def snapshot(self) -> dict[str, object]:
        """Structured UI-facing view; reading it never changes prompt delivery receipts."""
        items = DeliveryPolicy.items_for(self.view, DeliveryChannel.UI)
        return self.view.select(items).to_dict()

    def after_compact(self) -> None:
        PromptDelivery(self.root, self.session_id).after_compact()

    def close(self) -> None:
        PromptDelivery(self.root, self.session_id).clear()
