"""Small tmux lifecycle adapter for the automatic Board HUD sidecar."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from lib import config


HUD_HEIGHT = 3
MIN_WINDOW_HEIGHT = 45
OWNER_ENV = "DEVLOOP_HUD_OWNER"
SESSION_ENV = "DEVLOOP_HUD_SESSION"
LEADER_ENV = "DEVLOOP_HUD_LEADER_PANE"

RunTmux = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=1,
        check=False,
    )


def _pane_id(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if re.fullmatch(r"%\d+", value) else None


def _watch_command(plugin_root: Path, cwd: str, session_id: str, leader: str) -> str:
    launcher = plugin_root / "scripts" / "python"
    script = plugin_root / "scripts" / "board_hud.py"
    env = {
        OWNER_ENV: "1",
        SESSION_ENV: session_id,
        LEADER_ENV: leader,
    }
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    argv = " ".join(shlex.quote(str(value)) for value in (
        launcher,
        script,
        "--watch",
        "--cwd",
        cwd,
        "--session-id",
        session_id,
        "--leader-pane",
        leader,
    ))
    return f"exec env {assignments} {argv}"


def _owned_panes(output: str, session_id: str | None, leader: str) -> list[str]:
    wanted = [f"{OWNER_ENV}=1", f"{LEADER_ENV}={shlex.quote(leader)}"]
    if session_id is not None:
        wanted.append(f"{SESSION_ENV}={shlex.quote(session_id)}")
    found: list[str] = []
    for line in output.splitlines():
        pane, _, command = line.partition("\t")
        pane = _pane_id(pane) or ""
        if pane and all(marker in command for marker in wanted):
            found.append(pane)
    return found


def ensure_hud_pane(
    cwd: str,
    session_id: str,
    *,
    env: dict[str, str] | None = None,
    run_tmux: RunTmux = _default_run,
) -> str:
    """Ensure one three-line HUD for this CLI pane; all failures degrade to a no-op."""
    env = dict(os.environ if env is None else env)
    leader = _pane_id(env.get("TMUX_PANE"))
    if not env.get("TMUX") or not leader:
        return "skipped_not_tmux"
    if not config.board_hud(cwd).get("enabled", True):
        return "skipped_disabled"

    try:
        height_result = run_tmux(["display-message", "-p", "-t", leader, "#{window_height}"])
        try:
            window_height = int((height_result.stdout or "").strip())
        except ValueError:
            window_height = 0
        if 0 < window_height < MIN_WINDOW_HEIGHT:
            return "skipped_window_too_small"

        panes_result = run_tmux([
            "list-panes", "-t", leader, "-F", "#{pane_id}\t#{pane_start_command}",
        ])
        pane_output = panes_result.stdout or ""
        panes = _owned_panes(pane_output, session_id, leader)
        if panes:
            keeper, *duplicates = panes
            run_tmux(["resize-pane", "-t", keeper, "-y", str(HUD_HEIGHT)])
            for pane in duplicates:
                run_tmux(["kill-pane", "-t", pane])
            return "reused"

        # A new CLI session may reuse the same shell/tmux pane after the previous CLI exits.
        # Replace its stale HUD before creating this session's owner, rather than stacking panes.
        for pane in _owned_panes(pane_output, None, leader):
            run_tmux(["kill-pane", "-t", pane])

        root = config.plugin_root()
        command = _watch_command(root, cwd, session_id, leader)
        created = run_tmux([
            "split-window", "-v", "-l", str(HUD_HEIGHT), "-d", "-P", "-F", "#{pane_id}",
            "-t", leader, "-c", cwd, command,
        ])
        return "created" if _pane_id(created.stdout) else "failed"
    except (OSError, subprocess.SubprocessError):
        return "failed"


def pane_exists(pane_id: str, run_tmux: RunTmux = _default_run) -> bool:
    pane = _pane_id(pane_id)
    if not pane:
        return False
    try:
        result = run_tmux(["display-message", "-p", "-t", pane, "#{pane_id}"])
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and _pane_id(result.stdout) == pane


def pane_command(pane_id: str, run_tmux: RunTmux = _default_run) -> str | None:
    """Foreground command for a pane, or None once the leader can no longer be observed."""
    pane = _pane_id(pane_id)
    if not pane:
        return None
    try:
        result = run_tmux(["display-message", "-p", "-t", pane, "#{pane_current_command}"])
    except (OSError, subprocess.SubprocessError):
        return None
    command = (result.stdout or "").strip()
    return command if result.returncode == 0 and command else None
