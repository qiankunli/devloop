#!/usr/bin/env python3
"""One-shot **Wake** waiter for the review path — the no-channel half of event-driven resume
(see docs/event-driven-resume.md). Pairs with `review_channel.py`: same trigger, different
transport, so a session can pick either per its environment (channel = preview + `mcp`; this =
neither).

`run_review.py` (the *Perceive* layer, detached by the review lifecycle hook) writes the repo's
`.devloop/review.json`. This process is the *Wake* layer: it snapshots that review's **wake key**
— reusing `review_channel.wake_key`, so the waiter and the channel agree byte-for-byte on what
counts as wake-worthy (terminal + actionable: findings / file failures / error; never running /
skipped / clean) — polls until an actionable review whose key DIFFERS from the snapshot lands,
prints one line, and **exits**.

Why exit (not stay resident): a session arms this via `run_in_background`; its **exit** re-invokes
the session exactly once — a clean single wake. A resident task can't: the harness re-delivers a
running task's stdout every turn (anthropics/claude-code#66219); a one-shot task is terminal, so
nothing is re-delivered.

Why reuse `wake_key` rather than wake on any review.json write: a clean / running / skipped review
carries nothing to act on — token is the first constraint; those surface via the next-prompt pull
in `context/repo.py`, not a wake. Channel and waiter must NOT diverge on this, hence the shared key.

Deliberately dumb — it only signals "an actionable review landed", NOT the findings. The woken
*Execute* turn re-reads `.devloop/review.json` for the full set; that re-read is the price of the
no-preview path (the channel, by contrast, carries the findings inline).

Usage:
  wait_for_review_change.py <repo> [--interval SEC] [--timeout SEC]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
sys.path.insert(0, str(HERE))  # import the sibling review_channel for the shared wake_key

from lib import repo_layout  # noqa: E402
from lib.context import base  # noqa: E402
from review_channel import wake_key  # noqa: E402  (single source of truth for "actionable review")

DEFAULT_INTERVAL = 10.0   # poll the local review.json this often (the terminal write is one-time)
DEFAULT_TIMEOUT = 1800.0  # give up after this long (a bounded, re-armable wake; reviews run minutes)


def review_key(repo: str | Path):
    """The wake-worthy identity of the repo's current review, or None when there's nothing to wake
    for (missing / running / skipped / clean). The same function the channel pushes on."""
    return wake_key(base.load_segment(repo, "review"))


def wait_for_change(repo: str | Path, *, interval: float = DEFAULT_INTERVAL,
                    timeout: float = DEFAULT_TIMEOUT,
                    sleep=time.sleep, clock=time.monotonic) -> tuple[str, object]:
    """Block until an ACTIONABLE review whose key differs from the arm-time snapshot lands, or
    `timeout` elapses. Returns ("changed", new_key) or ("timeout", baseline_key). Only a non-None
    key (an actionable review) counts — a transition to running/clean (key → None) is NOT a wake,
    matching the channel's `key is not None` gate. `sleep`/`clock` are injectable for tests; never
    raises (a bad/missing segment reads as key None)."""
    baseline = review_key(repo)
    deadline = clock() + timeout
    while clock() < deadline:
        sleep(interval)
        cur = review_key(repo)
        if cur is not None and cur != baseline:
            return "changed", cur
    return "timeout", baseline


def _opt(argv: list[str], flag: str, default: float) -> float:
    if flag in argv:
        try:
            return float(argv[argv.index(flag) + 1])
        except (IndexError, ValueError):
            pass
    return default


def main(argv: list[str]) -> int:
    positional = [a for a in argv if not a.startswith("--")
                  and (not argv or argv[argv.index(a) - 1] not in ("--interval", "--timeout"))]
    target = positional[0] if positional else "."
    repo = repo_layout.find_git_root(target) or target
    reason, _ = wait_for_change(repo, interval=_opt(argv, "--interval", DEFAULT_INTERVAL),
                                timeout=_opt(argv, "--timeout", DEFAULT_TIMEOUT))
    # One line → the task's output file, which the session reads on wake. The reason is the whole
    # signal; Execute re-reads .devloop/review.json for the findings.
    tag = "review-change" if reason == "changed" else "review-watch-timeout"
    print(f"devloop: {tag} repo={repo}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
