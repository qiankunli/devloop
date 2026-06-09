#!/usr/bin/env python3
"""One-shot **Wake** waiter for event-driven resume (see docs/event-driven-resume.md).

The forge monitor (`poll_pr_status.py`) is the *Perceive* layer — it persists the repo's
PR/MR window to `.devloop/pr.json`. This process is the *Wake* layer: it snapshots that
segment's **semantic key** — the same `(pr_number, [(number, state)...])` the monitor uses
for `changed` — polls until the key differs from the snapshot, prints one line, and **exits**.

Why exit (not stay resident): a session arms this via `run_in_background`; its **exit**
re-invokes the session exactly once — a clean single wake. A long-lived monitor can't do
this — the harness re-delivers a resident task's stdout every turn
(anthropics/claude-code#66219); a one-shot task is terminal, so nothing is re-delivered.

Deliberately dumb — it only signals "the PR window changed", NOT *which* change or what to
do. Judging relevance + the next step is the *Execute* layer's job, after wake; Execute
re-reads `.devloop/pr.json` for the actual fresh state.

Usage:
  wait_for_pr_change.py <repo> [--interval SEC] [--timeout SEC]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import repo_layout  # noqa: E402
from lib.context import base  # noqa: E402

DEFAULT_INTERVAL = 15.0   # poll the local segment file this often (monitor writes ~every 90s)
DEFAULT_TIMEOUT = 1800.0  # give up after this long with no change (a bounded, re-armable wake)


def _key(seg: dict | None):
    """The monitor's `changed`-key for a `pr` segment: pr_number + each PR's (number, state).
    None when the segment is missing — so the segment first appearing also counts as a change."""
    if not seg:
        return None
    return (seg.get("pr_number"),
            tuple((p.get("number"), p.get("state")) for p in (seg.get("prs") or [])))


def segment_key(repo: str | Path):
    return _key(base.load_segment(repo, "pr"))


def wait_for_change(repo: str | Path, *, interval: float = DEFAULT_INTERVAL,
                    timeout: float = DEFAULT_TIMEOUT,
                    sleep=time.sleep, clock=time.monotonic) -> tuple[str, object]:
    """Block until the `pr` segment key changes from its snapshot, or `timeout` elapses.
    Returns ("changed", new_key) or ("timeout", baseline_key). `sleep`/`clock` are injectable
    for tests. Never raises (a bad/missing segment just reads as key None)."""
    baseline = segment_key(repo)
    deadline = clock() + timeout
    while clock() < deadline:
        sleep(interval)
        cur = segment_key(repo)
        if cur != baseline:
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
    # One line → the task's output file, which the session reads on wake. The reason is the
    # whole signal; Execute re-reads .devloop/pr.json for the actual state.
    tag = "pr-change" if reason == "changed" else "pr-watch-timeout"
    print(f"devloop: {tag} repo={repo}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
