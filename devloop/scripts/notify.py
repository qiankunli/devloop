#!/usr/bin/env python3
"""Unified entrypoint for the notify port — drive a wake `Source` through one of two transports.

  notify.py channel <source> <project_dir>                          # long-lived MCP channel
  notify.py waiter  <source> <repo> [--interval SEC] [--timeout SEC]  # one-shot background task

<source> ∈ lib/notify/sources.SOURCES (forge | review | …). `channel` watches every workspace
subproject and pushes events into the open session (needs the `mcp` package + the channels dev
flag — research preview). `waiter` watches ONE repo and exits on the first event; its exit
re-invokes the session (stdlib only — arm it via the run_in_background tool). Both consume the SAME
Source, so they never disagree on when to wake — see docs/event-driven-resume.md.

Examples:
  # channel (spawned by an mcpServers config):
  notify.py channel review "${CLAUDE_PROJECT_DIR}"
  # waiter (the session arms it in the background after launching a long action):
  notify.py waiter review /path/to/repo
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
sys.path.insert(0, str(HERE))  # reuse the monitor's repo resolution

from lib import repo_layout  # noqa: E402
from lib.notify.sources import SOURCES  # noqa: E402
from poll_pr_status import repos_to_poll  # noqa: E402  (reuse, no second poll)


def _opt(argv: list[str], flag: str, default: float) -> float:
    if flag in argv:
        try:
            return float(argv[argv.index(flag) + 1])
        except (IndexError, ValueError):
            pass
    return default


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in ("channel", "waiter") or argv[1] not in SOURCES:
        print(f"usage: notify.py {{channel|waiter}} {{{'|'.join(SOURCES)}}} <target> "
              "[--interval SEC] [--timeout SEC]", file=sys.stderr)
        return 2
    transport, source, rest = argv[0], SOURCES[argv[1]], argv[2:]
    target = next((a for a in rest if not a.startswith("--")), ".")

    if transport == "channel":
        import anyio

        from lib.notify.channel import run_channel
        anyio.run(run_channel, source, lambda: repos_to_poll(target))
        return 0

    # waiter: one repo, exit on first event (exit = the wake)
    import asyncio

    from lib.notify.waiter import DEFAULT_INTERVAL, DEFAULT_TIMEOUT, run_waiter
    repo = repo_layout.find_git_root(target) or target
    reason, _ = asyncio.run(run_waiter(
        source, repo,
        interval=_opt(rest, "--interval", DEFAULT_INTERVAL),
        timeout=_opt(rest, "--timeout", DEFAULT_TIMEOUT),
    ))
    # StdoutNotifier already printed the content (if any); add a stable tag line so the woken turn
    # (and the timeout case, which prints no content) has a marker. Execute reads .devloop/<seg>.json
    # for the full state.
    tag = f"{source.name}-change" if reason == "changed" else f"{source.name}-watch-timeout"
    print(f"devloop: {tag} repo={repo}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
