#!/usr/bin/env python3
"""Unified entrypoint for the notify port — drive a wake `Source` through a transport, or decide
whether to arm one.

  notify.py channel    <source> <project_dir>                           # long-lived MCP channel
  notify.py waiter     <source> <repo> [--interval SEC] [--timeout SEC]  # one-shot background task
  notify.py should-arm <source> <repo>                                  # capability probe (exit 0=arm / 1=skip)

<source> ∈ lib/notify/sources.SOURCES (forge | review | all | …); `all` (CompositeSource) watches
the whole bus. `channel` watches every workspace subproject and pushes events into the open session
(needs the `mcp` package + the channels dev flag — research preview). `waiter` watches ONE repo and
exits on the first event; its exit re-invokes the session (stdlib only — arm it via the
run_in_background tool). `should-arm` is the SYNCHRONOUS, non-waking decision a caller runs FIRST:
exit 0 (no standing channel) → arm a `waiter`; exit 1 (a `channel all` already covers the session,
per `config.notify().channels`) → skip, so a standing channel costs zero arming. All consume the
SAME Source — see docs/event-driven-resume.md.

Examples:
  # channel (spawned by an mcpServers config) — one standing channel over the whole bus:
  notify.py channel all "${CLAUDE_PROJECT_DIR}"
  # the action flow: probe foreground first, then arm a waiter in the background only on exit 0 —
  notify.py should-arm all /path/to/repo   # exit 0 → run_in_background: notify.py waiter all <repo>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
sys.path.insert(0, str(HERE))  # reuse the monitor's repo resolution

from lib import config, repo_layout  # noqa: E402
from lib.notify.sources import SOURCES  # noqa: E402
from poll_pr_status import repos_to_poll  # noqa: E402  (reuse, no second poll)


def _opt(argv: list[str], flag: str, default: float) -> float:
    if flag in argv:
        try:
            return float(argv[argv.index(flag) + 1])
        except (IndexError, ValueError):
            pass
    return default


def _channels_active(repo: str) -> bool:
    """Whether a standing `channel all` already covers this session — then `should-arm` says skip
    (no waiter needed). Explicit signal (channels are a session/account property the user knows),
    not a runtime probe: env DEVLOOP_NOTIFY_CHANNELS wins, else `config.notify().channels` (default
    False → arm the waiter, the always-available floor)."""
    env = os.environ.get("DEVLOOP_NOTIFY_CHANNELS")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(config.notify(repo).get("channels", False))


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in ("channel", "waiter", "should-arm") or argv[1] not in SOURCES:
        print(f"usage: notify.py {{channel|waiter|should-arm}} {{{'|'.join(SOURCES)}}} <target> "
              "[--interval SEC] [--timeout SEC]", file=sys.stderr)
        return 2
    transport, source, rest = argv[0], SOURCES[argv[1]], argv[2:]
    target = next((a for a in rest if not a.startswith("--")), ".")

    if transport == "channel":
        import anyio

        from lib.notify.channel import run_channel
        anyio.run(run_channel, source, lambda: repos_to_poll(target))
        return 0

    repo = repo_layout.find_git_root(target) or target

    if transport == "should-arm":
        # The capability decision, made BEFORE the caller spawns anything — synchronous and
        # non-waking. A standing `channel all` covers the session → skip (exit 1); otherwise a
        # backgrounded waiter would itself wake on its own exit just to report "nothing to do".
        if _channels_active(repo):
            print("skip: channels active — a standing channel covers the session; do not arm a waiter",
                  flush=True)
            return 1
        print("arm: no standing channel — arm a waiter (run_in_background) to be woken", flush=True)
        return 0

    # waiter: one repo, exit on first event (exit = the wake).
    import asyncio

    from lib.notify.waiter import DEFAULT_INTERVAL, DEFAULT_TIMEOUT, run_waiter
    reason, _ = asyncio.run(run_waiter(
        source, repo,
        interval=_opt(rest, "--interval", DEFAULT_INTERVAL),
        timeout=_opt(rest, "--timeout", DEFAULT_TIMEOUT),
    ))
    # StdoutNotifier already printed the content (if any); add a stable tag line so the woken
    # turn (and the timeout case, which prints no content) has a marker. Execute reads
    # .devloop/<seg>.json for the full state.
    tag = f"{source.name}-change" if reason == "changed" else f"{source.name}-watch-timeout"
    print(f"devloop: {tag} repo={repo}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
