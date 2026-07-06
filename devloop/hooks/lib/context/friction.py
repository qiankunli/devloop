"""Friction ledger — persist a policy-engine deny as a structured event.

The policy engine already computes a structured `Decision` (rule / severity / locator) on
every blocked tool call, then throws it away the moment it returns the deny message. This sink
is the one line that keeps it: append a `friction` record to `.devloop/friction.jsonl` (a
ledger — append-only, gitignored, per-clone). Those records are the raw material for the
loop's low-efficiency signals — a guard that keeps firing on legitimate work (a mis-scoped
protect pattern), a rule that's never right — that today only live in a human's memory.

Two invariants:
- **Best-effort, never load-bearing**: a logging failure MUST NOT change the guard verdict.
  The whole call is wrapped so a deny stays a deny even if the write blows up.
- **Branch, best-effort**: captured for later attribution to a requirement (the
  `requirements/` scope is a later slice — see docs/loop-state.md, this is slice 1). None
  when it can't be resolved; the miner tolerates it.
"""
from __future__ import annotations

from lib.core.domain import Decision, Severity

from .. import git_state, repo_layout
from . import base


def record_deny(decision: Decision, *, tool: str, cwd: str | None) -> None:
    """Append a friction event for a blocked `Decision`. No-op if not blocked. Never raises —
    the guard's verdict has already been decided; this only records why."""
    if not decision.blocked:
        return
    try:
        root = repo_layout.find_git_root(cwd or ".") or (cwd or ".")
        findings = [
            {"rule": f.rule, "locator": f.locator}
            for f in decision.findings
            if f.severity is Severity.DENY
        ]
        base.append_jsonl(root, "friction", {
            "kind": "friction",
            "ts": round(base.now(), 1),
            "source": "guard",          # policy-engine deny; gate/gitops ✗ are later sources
            "tool": tool,
            "branch": git_state.get_current_branch(root),
            "cwd": cwd,
            "findings": findings,
        })
    except Exception:
        pass  # best-effort: a friction-log failure must never break the guard
