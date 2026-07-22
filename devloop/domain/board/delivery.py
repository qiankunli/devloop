"""Delivery policy and per-session prompt receipts for Board views."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from domain.context import base, store
from domain.context.base import LABEL_NUDGE_CAP, REVIEW_NUDGE_CAP, SESSION_TTL_SEC, TURN_TTL_SEC
from domain.context.session import session_name

from .model import BoardItem, BoardItemType, BoardView
from .render import render_prompt


class DeliveryChannel(str, Enum):
    PROMPT = "prompt"
    UI = "ui"


class PromptScope(str, Enum):
    SESSION = "session"
    TURN = "turn"


class PromptTrigger(str, Enum):
    SESSION_START = "session_start"
    USER_PROMPT = "user_prompt"


@dataclass(frozen=True)
class DeliveryRule:
    channels: frozenset[DeliveryChannel]
    prompt_scope: PromptScope | None = None
    max_deliveries: int | None = None
    replay_after_compact: bool = True


_PROMPT_AND_UI = frozenset({DeliveryChannel.PROMPT, DeliveryChannel.UI})
_UI_ONLY = frozenset({DeliveryChannel.UI})


class DeliveryPolicy:
    """One policy table; fact producers never choose delivery behavior."""

    _rules = {
        BoardItemType.WORKSPACE: DeliveryRule(_PROMPT_AND_UI, PromptScope.SESSION),
        BoardItemType.REPO_REFERENCES: DeliveryRule(_PROMPT_AND_UI, PromptScope.SESSION),
        BoardItemType.REQUIREMENT_CURRENT: DeliveryRule(_PROMPT_AND_UI, PromptScope.SESSION),
        BoardItemType.REPO_IDENTITY: DeliveryRule(_PROMPT_AND_UI, PromptScope.TURN),
        BoardItemType.REPO_VALIDATION: DeliveryRule(_PROMPT_AND_UI, PromptScope.TURN),
        BoardItemType.REPO_PR_BLOCKED: DeliveryRule(
            _PROMPT_AND_UI, PromptScope.TURN, max_deliveries=1, replay_after_compact=False,
        ),
        BoardItemType.REPO_REVIEW: DeliveryRule(
            _PROMPT_AND_UI,
            PromptScope.TURN,
            max_deliveries=REVIEW_NUDGE_CAP,
            replay_after_compact=False,
        ),
        BoardItemType.REPO_REVIEW_LABEL: DeliveryRule(
            _PROMPT_AND_UI,
            PromptScope.TURN,
            max_deliveries=LABEL_NUDGE_CAP,
            replay_after_compact=False,
        ),
        BoardItemType.REPO_PR_HISTORY: DeliveryRule(_UI_ONLY),
    }

    @classmethod
    def rule_for(cls, item_type: BoardItemType) -> DeliveryRule:
        return cls._rules[item_type]

    @classmethod
    def items_for(
        cls,
        view: BoardView,
        channel: DeliveryChannel,
        prompt_scopes: frozenset[PromptScope] | None = None,
    ) -> tuple[BoardItem, ...]:
        """Select a channel without teaching Board items where they will be delivered."""
        return tuple(
            item
            for item in view.items
            if channel in (rule := cls.rule_for(item.type)).channels
            and (prompt_scopes is None or rule.prompt_scope in prompt_scopes)
        )

    @classmethod
    def validate(cls) -> None:
        missing = set(BoardItemType) - set(cls._rules)
        extra = set(cls._rules) - set(BoardItemType)
        if missing or extra:
            raise ValueError(f"Board delivery policy mismatch: missing={missing}, extra={extra}")


@dataclass
class _DeliveryMark:
    item_type: str = ""
    signature: str = ""
    count: int = 0
    last_emit_at: float | None = None

    @classmethod
    def from_dict(cls, value: dict | None) -> _DeliveryMark:
        value = value or {}
        return cls(
            item_type=str(value.get("item_type") or value.get("item_key") or ""),
            signature=str(value.get("signature") or ""),
            count=int(value.get("count", 0) or 0),
            last_emit_at=value.get("last_emit_at"),
        )


@dataclass
class DeliveryReceipt:
    items: dict[str, _DeliveryMark] = field(default_factory=dict)

    def due(self, item: BoardItem, rule: DeliveryRule, now: float) -> bool:
        mark = self.items.get(item.id)
        if mark is None or mark.signature != item.signature:
            return True
        if rule.max_deliveries is not None:
            return mark.count < rule.max_deliveries
        ttl = SESSION_TTL_SEC if rule.prompt_scope is PromptScope.SESSION else TURN_TTL_SEC
        return mark.last_emit_at is None or (now - mark.last_emit_at) >= ttl

    def mark(self, item: BoardItem, now: float) -> None:
        previous = self.items.get(item.id)
        same = previous is not None and previous.signature == item.signature
        self.items[item.id] = _DeliveryMark(
            item_type=item.type.value,
            signature=item.signature,
            count=(previous.count + 1 if same else 1),
            last_emit_at=now,
        )


class DeliveryReceiptStore:
    """Persistence adapter; receipts are disposable delivery state, not Board facts."""

    def __init__(self, root: str, session_id: str | None):
        self.root = root
        self.session_id = session_id

    def load(self) -> DeliveryReceipt:
        raw = store.load_segment(
            self.root,
            f"board/sessions/{session_name(self.session_id)}",
        ) or {}
        return DeliveryReceipt(
            items={
                item_id: _DeliveryMark.from_dict(value)
                for item_id, value in (raw.get("items") or {}).items()
            }
        )

    def save(self, receipt: DeliveryReceipt) -> None:
        store.save_segment(
            self.root,
            f"board/sessions/{session_name(self.session_id)}",
            asdict(receipt),
        )

    def clear(self) -> None:
        try:
            store.segment_file(
                self.root,
                f"board/sessions/{session_name(self.session_id)}",
            ).unlink(missing_ok=True)
        except OSError:
            pass


class PromptDelivery:
    def __init__(self, root: str, session_id: str | None):
        self.receipts = DeliveryReceiptStore(root, session_id)

    def deliver(
        self,
        view: BoardView,
        trigger: PromptTrigger = PromptTrigger.USER_PROMPT,
    ) -> str | None:
        receipt = self.receipts.load()
        now = base.now()
        due: list[BoardItem] = []
        for item in view.items:
            rule = DeliveryPolicy.rule_for(item.type)
            if DeliveryChannel.PROMPT not in rule.channels:
                continue
            if trigger is PromptTrigger.SESSION_START and rule.prompt_scope is not PromptScope.SESSION:
                continue
            if receipt.due(item, rule, now):
                due.append(item)
        if not due:
            return None
        for item in due:
            receipt.mark(item, now)
        self.receipts.save(receipt)
        return render_prompt(due)

    def after_compact(self) -> None:
        receipt = self.receipts.load()
        changed = False
        for item_id, mark in receipt.items.items():
            try:
                # Old receipts used the Board item type directly; keep them harmless on upgrade.
                item_type = mark.item_type or item_id
                rule = DeliveryPolicy.rule_for(BoardItemType(item_type))
            except (KeyError, ValueError):
                continue
            if rule.replay_after_compact:
                mark.signature = ""
                mark.count = 0
                mark.last_emit_at = None
                changed = True
        if changed:
            self.receipts.save(receipt)

    def clear(self) -> None:
        self.receipts.clear()


DeliveryPolicy.validate()
