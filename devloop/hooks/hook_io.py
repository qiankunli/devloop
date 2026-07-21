"""Hook I/O harness — centralizes the boilerplate every devloop hook repeats.

A hook script used to re-implement, every time: read stdin JSON, guard against
parse errors, figure out the no-op `{}` output, and hand-build the decision-output
JSON shape. Here that lives once. A hook becomes a single `decide` / `produce` /
`handle` function handed to one of the runners below.

Two guarantees the runners enforce so a hook can stay 3 lines:
- **Never break the user's tool call.** Any exception in the user function is
  swallowed and degraded to the safe default (guard → allow, inject → nothing).
  A buggy guard must fail *open*, never wedge the session.
- **CLI-agnostic by construction.** Reads only the payload subset shared across
  Claude Code and (future) Codex — `hook_event_name` / `tool_name` / `tool_input`
  / `cwd` — and emits only shared output fields. No plugin-root env var is read
  here; hook scripts self-locate via `sys.path.insert`. So the same script runs
  on either CLI once Codex's hook schema lands; see plan §2.4.

Usage (a guard)::

    from hooks import hook_io

    def decide(inp: hook_io.HookInput) -> str | None:
        if inp.is_tool("Bash") and "git push" in inp.command and on_protected():
            return "Refusing push on a protected branch."
        return None

    if __name__ == "__main__":
        raise SystemExit(hook_io.guard(decide))
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class HookInput:
    event: str
    tool_name: str
    tool_input: dict
    cwd: str
    raw: dict = field(repr=False)

    @property
    def command(self) -> str:
        """Bash command string ('' for non-Bash tools)."""
        return (self.tool_input or {}).get("command", "") or ""

    @property
    def session_id(self) -> str:
        """Stable per-CLI-session id ('' if the CLI doesn't provide one)."""
        return self.raw.get("session_id", "") or ""

    @property
    def is_codex(self) -> bool:
        """Codex hook payloads expose `model`; Claude's shared payload subset does not."""
        return "model" in self.raw

    @property
    def file_path(self) -> str:
        """Edited file's path for Edit/Write/NotebookEdit ('' otherwise).

        Edit-family hooks must resolve the repo from THIS, not `cwd` — in an
        aggregate workspace the session cwd sits at the workspace root while
        edits land inside subprojects, so a cwd-based lookup silently no-ops.
        """
        return (self.tool_input or {}).get("file_path") or (self.tool_input or {}).get("notebook_path") or ""

    def edited_dir(self) -> str:
        """Directory containing the edited file (relative paths anchored at cwd),
        falling back to cwd for non-edit tools."""
        fp = self.file_path
        return str((Path(self.cwd) / fp).parent) if fp else self.cwd

    def is_tool(self, *names: str) -> bool:
        return self.tool_name in names


def read_input() -> HookInput:
    """Parse the hook payload from stdin. Malformed/empty → an all-default HookInput."""
    try:
        raw = json.loads(sys.stdin.read() or "{}")
        if not isinstance(raw, dict):
            raw = {}
    except (json.JSONDecodeError, ValueError):
        raw = {}
    tool_input = raw.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        # Codex freeform tools can surface a string payload. Keep the common dict API
        # for hook logic by preserving it under a neutral key instead of dropping it.
        tool_input = {"input": str(tool_input)}
    return HookInput(
        event=raw.get("hook_event_name", "") or "",
        tool_name=raw.get("tool_name", "") or "",
        tool_input=tool_input,
        cwd=raw.get("cwd") or str(Path.cwd()),
        raw=raw,
    )


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))


# ── runners: one per hook role; each is exception-safe (fail to the safe default) ──

def guard(decide: Callable[[HookInput], Optional[str]], event: str = "PreToolUse") -> int:
    """PreToolUse-style guard. `decide` returns a deny-reason string, or None to allow.

    Exception in `decide` → allow (fail-open: a buggy guard must never block the user).
    """
    inp = read_input()
    try:
        reason = decide(inp)
    except Exception:
        reason = None
    if reason:
        _emit({
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
            "systemMessage": reason,
        })
    else:
        _emit({})
    return 0


def inject(produce: Callable[[HookInput], Optional[str]], event: str) -> int:
    """UserPromptSubmit / SessionStart-style injector. `produce` returns the context
    text to inject, or None for nothing. Exception → inject nothing.

    `event` must match the firing event (`UserPromptSubmit` / `SessionStart` / ...)
    so `additionalContext` is attributed correctly.
    """
    inp = read_input()
    try:
        ctx = produce(inp)
    except Exception:
        ctx = None
    if ctx:
        _emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}})
    else:
        _emit({})
    return 0


def run(build: Callable[[HookInput], Optional[dict]], event: str) -> int:
    """Generic runner for hooks whose output is a richer `hookSpecificOutput`
    payload (e.g. SessionStart returning `additionalContext` + `watchPaths`).

    `build` returns the payload dict (WITHOUT `hookEventName`, added here), or
    None for a no-op. Exception → no-op. This is the escape hatch when `guard`/
    `inject`/`observe` don't fit; prefer those three for the common cases.
    """
    inp = read_input()
    try:
        payload = build(inp)
    except Exception:
        payload = None
    if payload:
        _emit({"hookSpecificOutput": {"hookEventName": event, **payload}})
    else:
        _emit({})
    return 0


def observe(handle: Callable[[HookInput], None]) -> int:
    """PostToolUse / lifecycle side-effect hook (refresh state, schedule, etc.).

    `handle` does its work and returns nothing; we always emit `{}` and swallow any
    exception. Use for the (b)/(c) behaviors that touch state but make no decision.
    """
    inp = read_input()
    try:
        handle(inp)
    except Exception:
        pass
    _emit({})
    return 0
