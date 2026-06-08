"""Producer-side event seam for NON-hook sources (monitors / pollers).

CC-native changes reach devloop as hook payloads, normalized by `hook_io` (the
hook-wire-protocol adapter). External systems — a forge, a dev deploy, a verdict run —
have **no** native CC event; a monitor *synthesizes* one by polling. This module is the
thin counterpart to `hook_io` on that producer side: normalize "something changed" into
an `Event`, fan it out to `Handler`s, with the same fail-safe guarantee — one handler's
failure never blocks the others or crashes the monitor loop.

Deliberately minimal (an `Event` shape + `dispatch`), and on purpose:
- **No global registry / subscription magic.** Handlers are passed explicitly by the
  producer — what fires is readable at the call site, nothing hidden.
- **Not a wake / auto-continue policy.** `dispatch` only runs handlers; whether a handler
  emits a notification (wakes the agent), dedups, or matches a pending intent is the
  *handler's* business, kept out of here.
- **A seam, not a framework.** The forge PR-sweep monitor (`scripts/poll_pr_status.py`)
  is the producer; deploy / verdict sources plug into the same persist/notify fan-out
  instead of each re-inventing it.

Boundary vs `hook_io`: they are siblings, not nested. `hook_io` adapts the CC hook wire
protocol (push, rich payload incl. permission_mode); `events` normalizes pull-polled
external state (no session, no mode). Hook payloads don't route through here — the two
producer sides stay distinct.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Event:
    source: str                                   # producer kind: "forge" | "deploy" | "verdict" | ...
    type: str                                     # what happened: "pr.update" | ...
    subject: str                                  # what it's about (repo dir / scope / branch)
    payload: dict = field(default_factory=dict)   # source-specific data (e.g. the `pr` segment body)
    summary: str = ""                             # human one-liner for notify/wake handlers ("" = nothing to say)
    changed: bool = True                          # a real transition vs a no-op refresh tick


Handler = Callable[[Event], None]


def dispatch(event: Event, handlers: list[Handler]) -> None:
    """Fan `event` out to each handler in order, isolating failures: one handler raising
    never blocks the rest and never crashes the producer loop (mirrors `hook_io`'s
    fail-safe guarantee on the hook side)."""
    for handler in handlers:
        try:
            handler(event)
        except Exception:
            pass
