"""Board — structured collaboration context plus independent delivery policy."""
from .delivery import (
    DeliveryChannel,
    DeliveryPolicy,
    DeliveryReceipt,
    DeliveryReceiptStore,
    DeliveryRule,
    PromptDelivery,
    PromptScope,
    PromptTrigger,
)
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
from .projection import project_board, project_view
from .render import render_item, render_prompt
from .runtime import BoardRuntime
