"""Project independently-owned facts into typed Board items."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from domain.forge import MergeReadiness, pr_label
from lib import git_state

from domain.context import base, store
from domain.context.loopstate import requirement

from .model import (
    Board,
    BoardFocus,
    BoardItem,
    BoardItemType,
    BoardItemKind,
    BoardScope,
    BoardView,
    ComponentValidationCard,
    PrBlockedCard,
    PrHistoryCard,
    PullRequestCard,
    ReferenceCard,
    RepoIdentityCard,
    RepoReferencesCard,
    ReviewCard,
    ReviewLabelCard,
    SubprojectCard,
    TextCard,
    ValidationCard,
    WorkspaceCard,
)

if TYPE_CHECKING:
    from domain.context.repo import RepoContext
    from domain.context.workspace import WorkspaceContext


def project_board(
    root: str,
    workspace: WorkspaceContext | None = None,
    repos: tuple[RepoContext, ...] = (),
    stale_binding_hours: dict[str, float] | None = None,
) -> Board:
    items: list[BoardItem] = []
    if workspace:
        workspace_item = _workspace_item(root, workspace)
        if workspace_item:
            items.append(workspace_item)
    stale_binding_hours = stale_binding_hours or {}
    for repo in repos:
        items.extend(_repo_items(root, repo, stale_binding_hours.get(repo.repo.repo_dir)))
    return Board(root=root, items=tuple(items))


def project_view(
    root: str,
    workspace: WorkspaceContext | None = None,
    repo: RepoContext | None = None,
    stale_binding_hours: float | None = None,
) -> tuple[Board, BoardView]:
    repos = (repo,) if repo else ()
    stale = {repo.repo.repo_dir: stale_binding_hours} if repo and stale_binding_hours is not None else {}
    board = project_board(root, workspace, repos, stale)
    focus = BoardFocus(
        workspace_root=root,
        repo_root=repo.repo.repo_dir if repo else None,
    )
    return board, board.view(focus)


def _workspace_item(root: str, workspace: WorkspaceContext) -> BoardItem | None:
    references = tuple(_reference(reference) for reference in workspace.agents_md.references)
    subprojects = tuple(
        SubprojectCard(
            name=sub.name,
            aliases=tuple(sub.aliases),
            language=sub.language or "",
            role=sub.role or "",
            canonical=sub.canonical or "",
        )
        for sub in workspace.subprojects
    )
    if not references and not subprojects:
        return None
    return BoardItem(
        type=BoardItemType.WORKSPACE,
        kind=BoardItemKind.STATE,
        scope=BoardScope(root),
        payload=WorkspaceCard(workspace.workspace_root, references, subprojects),
    )


def _repo_items(root: str, repo: RepoContext, stale_binding_hours: float | None) -> list[BoardItem]:
    scope = BoardScope(root, repo.repo.repo_dir)
    items: list[BoardItem] = []
    if repo.agents_md.references:
        items.append(BoardItem(
            type=BoardItemType.REPO_REFERENCES,
            kind=BoardItemKind.STATE,
            scope=scope,
            payload=RepoReferencesCard(tuple(_reference(ref) for ref in repo.agents_md.references)),
        ))
    items.extend((
        BoardItem(
            type=BoardItemType.REPO_IDENTITY,
            kind=BoardItemKind.STATE,
            scope=scope,
            payload=_repo_identity(repo, stale_binding_hours),
        ),
        BoardItem(
            type=BoardItemType.REPO_VALIDATION,
            kind=BoardItemKind.STATE,
            scope=scope,
            payload=ValidationCard(tuple(
                ComponentValidationCard(
                    component=component,
                    lint_at=repo.validation.components[component].last_lint_at,
                    test_at=repo.validation.components[component].last_test_at,
                )
                for component in sorted(repo.validation.components)
            )),
        ),
    ))

    blocked = _blocked_pr(repo, scope)
    if blocked:
        items.append(blocked)
    review = _review(repo, scope)
    if review:
        items.append(review)
    if repo.label_pending:
        items.append(BoardItem(
            type=BoardItemType.REPO_REVIEW_LABEL,
            kind=BoardItemKind.EVENT,
            scope=scope,
            payload=ReviewLabelCard(repo.label_pending, repo.label_pending_key),
        ))

    # Requirement-first is intentionally deferred. Keep the current branch-scoped summary as
    # an explicit text bridge so Board can evolve without pretending this is its final model.
    requirement_line = requirement.turn_line(repo.repo.repo_dir, repo.branch.local.name or None)
    if requirement_line:
        items.append(BoardItem(
            type=BoardItemType.REQUIREMENT_CURRENT,
            kind=BoardItemKind.STATE,
            scope=scope,
            payload=TextCard(source="requirement.current", text=requirement_line),
        ))
    if repo.prs:
        items.append(BoardItem(
            type=BoardItemType.REPO_PR_HISTORY,
            kind=BoardItemKind.DETAIL,
            scope=scope,
            payload=PrHistoryCard(
                provider=repo.provider,
                pull_requests=tuple(
                    PullRequestCard(
                        number=pr.number,
                        state=pr.state,
                        source_branch=pr.source_branch,
                        target_branch=pr.target_branch,
                        title=pr.title,
                        web_url=pr.web_url,
                    )
                    for pr in repo.prs
                ),
            ),
        ))
    return items


def _reference(reference) -> ReferenceCard:
    return ReferenceCard(
        title=reference.title,
        path=reference.path,
        description=(reference.hook or "").strip(),
    )


def _repo_identity(repo: RepoContext, stale_binding_hours: float | None) -> RepoIdentityCard:
    branch = repo.branch
    checkout = repo.repo.real_repo_dir or repo.repo.repo_dir
    base_branch = branch.base_branch()
    ahead, behind = git_state.get_ahead_behind(checkout, base_branch) or (0, 0)
    remote = branch.remote_tip(base_branch)
    trunk_moved = False
    if remote and remote.commit:
        local_mirror = git_state.rev_parse(checkout, f"origin/{base_branch}")
        trunk_moved = bool(local_mirror and local_mirror != remote.commit)
    dirty = git_state.get_workspace_status(checkout)
    current_pr = repo.current_pr()
    lifecycle = ""
    if current_pr and current_pr.inactive:
        lifecycle = "inactive"
    elif current_pr and current_pr.is_open:
        lifecycle = "in_flight"
    return RepoIdentityCard(
        code_dir=_display_code_dir(repo),
        language=repo.repo.language or "",
        branch=branch.local.name or "",
        linked_worktree=git_state.is_linked_worktree(repo.repo.real_repo_dir),
        ahead=ahead,
        behind=behind,
        base_branch=base_branch,
        remote_checked_at=branch.remotes_fetched_at,
        trunk_moved_since_fetch=trunk_moved,
        target_branch=branch.target,
        workspace_dirty=bool(dirty.get("dirty")),
        modified_count=int(dirty.get("modified_count", 0)),
        untracked_count=int(dirty.get("untracked_count", 0)),
        protected=branch.local.is_protected(),
        pr_lifecycle=lifecycle,
        pr_label=pr_label(repo.provider, current_pr.number) if current_pr else "",
        pr_state=current_pr.state if current_pr else "",
        stale_binding_hours=stale_binding_hours,
    )


def _blocked_pr(repo: RepoContext, scope: BoardScope) -> BoardItem | None:
    current_pr = repo.current_pr()
    if not current_pr or not current_pr.is_open or not repo.merge_readiness:
        return None
    try:
        readiness = MergeReadiness(repo.merge_readiness)
    except ValueError:
        return None
    if not readiness.blocks_merge:
        return None
    return BoardItem(
        type=BoardItemType.REPO_PR_BLOCKED,
        kind=BoardItemKind.EVENT,
        scope=scope,
        payload=PrBlockedCard(pr_label(repo.provider, current_pr.number), readiness.value),
    )


def _review(repo: RepoContext, scope: BoardScope) -> BoardItem | None:
    raw = store.load_segment(
        repo.repo.repo_dir,
        store.branch_segment(repo.branch.local.name or None, "review"),
    ) or {}
    status = _review_status(raw)
    if not status or not raw.get("reviewed_sha"):
        return None
    return BoardItem(
        type=BoardItemType.REPO_REVIEW,
        kind=BoardItemKind.EVENT,
        scope=scope,
        payload=ReviewCard(
            status=status,
            reviewed_sha=str(raw.get("reviewed_sha") or ""),
            findings=int(raw.get("count", 0) or 0),
            failed_files=int(raw.get("failed", 0) or 0),
        ),
    )


def _review_status(raw: dict) -> str:
    status = raw.get("status")
    if not status or status == "skipped":
        return ""
    if status == "running" and (base.now() - (raw.get("generated_at") or 0)) > base.REVIEW_STALE_SEC:
        return "stale"
    return str(status)


def _display_code_dir(repo: RepoContext) -> str:
    code_dir = Path(repo.repo.code_dir or repo.repo.real_repo_dir or repo.repo.repo_dir)
    checkout = Path(repo.repo.real_repo_dir or repo.repo.repo_dir or code_dir)
    main = store.state_dir(checkout).parent
    try:
        relative = code_dir.resolve().relative_to(checkout.resolve())
    except (OSError, ValueError):
        return str(code_dir)
    return str(main / relative)
