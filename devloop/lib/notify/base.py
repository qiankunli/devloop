"""The notify abstraction — three ports that stay mutually decoupled:

- `Notification`: what reaches the session (content + coarse kind + meta). Says *what* to surface,
  never *how* to deliver.
- `Notifier`: *how* a notification is delivered. `channel.ChannelNotifier` (push into an open
  session, content inline, multi-wake) and `waiter.StdoutNotifier` (print to a one-shot task's
  stdout, whose exit re-invokes the session) are the two backends; both carry content.
- `Source`: *what* to watch and *when* to fire. A source watches one repo's slice of the
  `.devloop/` state bus and emits `Notification`s. The SAME source drives both transports, so they
  can never disagree on when to wake — see package docstring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Notification:
    """A session-facing message: the `content` the Claude session should see, a coarse `kind`
    for routing/grouping, and free-form `meta`. Sources build these; they say *what* to surface,
    never *how* to deliver it."""

    content: str
    kind: str = "info"  # "pr_change" | "merge_blocked" | "review_done" | "deploy" | …
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    """Delivers a `Notification` to the session.

    Two backends today: `channel.ChannelNotifier` (push + wake an open session, content inline) and
    `waiter.StdoutNotifier` (print to a one-shot task's stdout; the task's exit re-invokes the
    session, content on that stdout). Both carry content; a future payload-less backend (e.g.
    `claude notify --type wake`) can implement the same port and legitimately drop `content`,
    letting the woken turn re-read state.
    """

    async def deliver(self, notification: Notification) -> None: ...


class Source(Protocol):
    """A wake source: watches one repo's slice of the state bus and decides when an event fires.

    `name` doubles as the channel `source=` attribute and the waiter tag; `instructions` is the
    channel handshake brief. Detection is a pure (carry → carry + fired) step, so the SAME source
    drives both transports identically — the channel pumps it forever (multi-wake), the waiter steps
    it until the first fire then exits (single-wake). `carry` is opaque per-repo state threaded
    across steps (the last change-key, or forge's hysteresis blocker).

    - `seed(repo)`: the carry for `repo`'s CURRENT state, so an event that predates the watch does
      not fire on startup (ignore-the-startup-edge).
    - `step(repo, carry)`: advance `repo`'s carry one tick; return `(new_carry, fired)`. Reads the
      bus, never delivers — so the trigger logic is unit-testable without a runner.
    """

    name: str
    instructions: str

    def seed(self, repo: str) -> object: ...
    def step(self, repo: str, carry: object) -> tuple[object, list[Notification]]: ...
