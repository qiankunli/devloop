"""Context state layer — shared leaves, constants, persistence primitives.

The `.devloop/` state bus spans two levels. **Repo** state is split into per-owner
*segment* files (`meta`/`branch`/`mr`/`validation`/`injection`.json) so independent
writer-roles never share a file — see `repo.py`. **Workspace** state is a single
`context.json` (one writer-role: the refresh). This module holds what both share:
leaf dataclasses (`Reference`, `AgentsMd`) plus the re-exported forge
domain (`PullRequest`), the injection
`Cadence` (content-hash dedup with a TTL safety net), and tunable constants.
The storage primitives (paths, three-domain resolution, atomic JSON I/O) live in
`store.py` — this module is the shared VOCABULARY, that one is the shared DISK.

The composite `RepoContext` / `WorkspaceContext` (with their git/AGENTS.md refresh
logic) live in `repo.py` / `workspace.py` and build on these.

Design notes that matter:
- **Cadence dedup** is how prompt injection stays cheap: re-emit only when the
  text changed, or when the TTL elapsed. `PostCompact` calls `Cadence.clear()` so
  state is re-injected right after compaction drops it — replacing a
  guess-with-a-timer TTL net with a native trigger (TTL stays as a backstop).
- All tunables (TTLs, MR cap/poll) are **constants here**, never in docs — they
  drift with experience and belong next to the code that reads them.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime

# The neutral code-review proposal lives in the forge domain (provider-agnostic). The
# state layer persists it and joins it by number; it does not redefine it.
from lib.forge.base import PRS_CAP, Comment, MergeReadiness, PullRequest, pr_label, vocab  # noqa: F401

# ── tunables (seconds unless noted) ──────────────────────────────────────────
REPO_STALE_SEC = 300          # repo context older than this → refresh_all on next cd/prompt
WORKSPACE_STALE_SEC = 600     # workspace context staleness
TURN_TTL_SEC = 1800           # turn-cadence injection re-emit backstop (~30 min)
SESSION_TTL_SEC = 14400       # session-cadence (References) re-emit backstop (~4 h)
PR_POLL_INTERVAL_SEC = 90     # monitor poll cadence for PR/MR status
REMOTE_VIEW_STALE_SEC = 120   # remote_branches snapshot older than this → re-pull trunk tips on enter
ACTIVE_REPO_TTL_SEC = 21600   # workspace last-active repo validity (~6 h); stale → don't guess
REVIEW_STALE_SEC = 1800       # review.json stuck at "running" longer than this (~30 min) → run_review
REQUIREMENT_STALE_SEC = 1209600  # requirement idle this long with no close (~14 d) → session_end assumed_done
                              # almost certainly died mid-flight (sleep/OOM/kill); surface as stale, not running
REQ_VIEW_PRS_CAP = 8          # requirement turn-line PR list cap (a requirement rarely has more in flight)
DEFAULT_BRANCH_TTL_SEC = 86400  # repo default branch is near-immutable → only re-fetch from the forge
                                # once a day (refresh_all runs far more often; this gates the network call)
LABEL_NUDGE_CAP = 3           # times to ask for a verdict on the SAME pending finding set, then
                              # go quiet — see Nudge. Not ignoring you: 3 asks is enough to have
                              # been heard, and labeling is advisory (ground truth, never a blocker).
REVIEW_NUDGE_CAP = 1          # times to report the SAME review result. One: it's an event, not
                              # state — re-telling it makes the agent re-triage findings it已
                              # 处理过。A re-run (new sha/status/counts) is a new event → tells again.

# ── leaf dataclasses ─────────────────────────────────────────────────────────
@dataclass
class Reference:
    """One entry from an AGENTS.md References section."""
    title: str
    path: str
    hook: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Reference":
        return cls(title=d.get("title", ""), path=d.get("path", ""), hook=d.get("hook"))


@dataclass
class AgentsMd:
    path: str | None = None
    references: list[Reference] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentsMd":
        return cls(
            path=d.get("path"),
            references=[Reference.from_dict(r) for r in (d.get("references") or [])],
        )


@dataclass
class Nudge:
    """A bounded chore reminder: ask `cap` times per situation, then go quiet until the
    situation actually changes.

    `Cadence` can't express this, which is why this exists alongside it. Cadence dedups on the
    WHOLE turn block's hash, so any unrelated line moving (HEAD sha, PR state) re-emits
    everything inside it — during active work that's every turn. That's right for state lines
    (they describe where you ARE, and a stale one would mislead) and wrong for a chore line (it
    asks you to DO something; the 4th ask carries nothing the 3rd didn't, and not acting is
    itself an answer).

    `key` identifies the situation. A new key = genuinely new work → count resets and it speaks
    up again; an unchanged key decays to silence. Level-triggered display, edge-triggered voice.
    """
    key: str = ""
    count: int = 0

    def due(self, key: str, *, cap: int) -> bool:
        """Should the nudge speak? New situation → yes; same one → only until `cap` asks."""
        return key != self.key or self.count < cap

    def bump(self, key: str) -> None:
        """Record one ask. A key change restarts the count at 1 (not 0 — this IS an ask)."""
        self.count = self.count + 1 if key == self.key else 1
        self.key = key

    @classmethod
    def from_dict(cls, d: dict | None) -> "Nudge":
        d = d or {}
        return cls(key=str(d.get("key") or ""), count=int(d.get("count", 0) or 0))


@dataclass
class Cadence:
    """One injection cadence's dedup stamp (turn or session).

    `should_emit` returns True when the content changed OR the TTL backstop elapsed.
    `clear` (called by the PostCompact hook) forces a re-emit next turn by dropping
    the stamp — so compaction can't silently strip injected state.
    """
    last_hash: str | None = None
    last_emit_at: float | None = None

    def should_emit(self, text: str, *, now: float, ttl: float) -> bool:
        if not text:
            return False
        if _content_hash(text) != self.last_hash:
            return True
        if self.last_emit_at is None or (now - self.last_emit_at) >= ttl:
            return True
        return False

    def mark(self, text: str, *, now: float) -> None:
        self.last_hash = _content_hash(text)
        self.last_emit_at = now

    def clear(self) -> None:
        self.last_hash = None
        self.last_emit_at = None

    @classmethod
    def from_dict(cls, d: dict) -> "Cadence":
        return cls(last_hash=d.get("last_hash"), last_emit_at=d.get("last_emit_at"))


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ── time helpers ─────────────────────────────────────────────────────────────
# All persisted timestamps are float epoch seconds (one representation, no ISO
# parsing / timezone bugs). `fmt_ts` renders them for prompt display only.
def now() -> float:
    return time.time()


def fmt_ts(ts: float | None) -> str:
    """An ABSOLUTE timestamp — deliberately not "5m ago", however much friendlier that reads.

    These render into the turn block, whose dedup (`Cadence`) hashes the WHOLE block: one
    clock-relative string would re-hash it every turn, so EVERY line would re-inject forever
    and the dedup would be silently dead — each line still correct, nothing failing. That
    invariant is pinned by test_turn_block_stable_across_clock_when_state_unchanged.
    """
    if not ts:
        return "never"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "never"


def is_stale(ts: float | None, ttl: float, *, now_: float | None = None) -> bool:
    """True if `ts` is missing or older than `ttl` seconds."""
    if ts is None:
        return True
    return ((now_ if now_ is not None else now()) - ts) >= ttl
