"""Waiter transport for the notify port — the no-channel, no-`mcp` Wake path.

A session arms `run_waiter` via the run_in_background tool; it steps a `Source` until the first
event fires, the `StdoutNotifier` prints that event's content, and the process **exits** — which
re-invokes the session exactly once, WITH the content inline on the task's stdout. Pairs with
`channel.run_channel` over the SAME `Source`, so the two transports never disagree on when to wake;
pick either per environment (channel = research preview + `mcp`; waiter = stdlib only).

Why exit (not stay resident): the harness re-delivers a RUNNING background task's stdout every turn
(anthropics/claude-code#66219); a one-shot task is terminal, so the wake fires exactly once. Why
single-wake (vs the channel's multi-wake): a one-shot process can only fire once — to keep watching,
the woken turn re-arms a fresh waiter. That asymmetry is inherent to the platform, not papered over.

Uses stdlib `asyncio` (not `anyio`/`mcp`) so the fallback path carries no preview/runtime deps.
"""
from __future__ import annotations

import time

from lib.notify.base import Source

DEFAULT_INTERVAL = 10.0   # poll the local state-bus file this often (the terminal write is one-time)
DEFAULT_TIMEOUT = 1800.0  # give up after this long (a bounded, re-armable wake; reviews run minutes)


class StdoutNotifier:
    """Delivers a `Notification` by printing its content to stdout — the wake payload of the
    one-shot waiter task, which the session reads when the task's exit re-invokes it."""

    async def deliver(self, notification) -> None:
        print(notification.content, flush=True)


async def run_waiter(source: Source, repo: str, *, interval: float = DEFAULT_INTERVAL,
                     timeout: float = DEFAULT_TIMEOUT, notifier=None,
                     sleep=None, clock=time.monotonic) -> tuple[str, object | None]:
    """Step `source` over `repo` until it fires — deliver via `notifier` (default `StdoutNotifier`)
    and return ("changed", first_note) — or `timeout` elapses → ("timeout", None). Seeds the carry
    from current state so a pre-existing event doesn't fire on startup. `sleep`/`clock` are
    injectable for tests (default `asyncio.sleep` / `time.monotonic`); never raises (a bad/missing
    segment just reads as 'no fire')."""
    if sleep is None:
        import asyncio
        sleep = asyncio.sleep
    notifier = notifier or StdoutNotifier()
    carry = source.seed(repo)
    deadline = clock() + timeout
    while clock() < deadline:
        await sleep(interval)
        carry, notes = source.step(repo, carry)
        if notes:
            for n in notes:
                await notifier.deliver(n)
            return "changed", notes[0]
    return "timeout", None
