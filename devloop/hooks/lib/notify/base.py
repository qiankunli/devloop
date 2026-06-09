"""The notify abstraction: a `Notification` (what reaches the session) + the `Notifier` port
(how it's delivered). Source-agnostic and delivery-agnostic — see package docstring."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Notification:
    """A session-facing message: the `content` the Claude session should see, a coarse `kind`
    for routing/grouping, and free-form `meta`. Producers (forge / deploy / verdict) build
    these; they say *what* to surface, never *how* to deliver it."""

    content: str
    kind: str = "info"  # "pr_change" | "deploy" | "verdict" | …
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    """Delivers a `Notification` to the session.

    `channel.ChannelNotifier` (push content + wake) is the first implementation. A payload-less
    backend — the one-shot-waiter fallback, or a future `claude notify --type wake` — can
    implement the same port; it delivers only the wake and lets the woken turn re-read state
    for the detail (so `content` may be dropped by such a backend, by design).
    """

    async def deliver(self, notification: Notification) -> None: ...
