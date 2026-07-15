"""Review feedback — which published findings still lack a verdict.

The durable relationship, entirely on the forge: `run_review` publishes each finding as an
ANCHORED comment carrying a `ccr:fp=<fp>` footer; an agent/human later replies in that thread
with `ccr:label=<verdict>`. This module joins the two back together over `Forge.comments()`.

Nothing here is persisted as a source of truth, on purpose. The join key (`fp`) travels inside
the comment bodies, so the pair is recoverable from the API alone — from any machine, any
worktree, any session, including ones that never saw the review run. A local fp→comment-id
table would only be a staler copy of what `comments()` already returns, and would silently
break the join when it's lost. Callers may CACHE a derived count (see `context.prstate`), which
is safe precisely because it can always be re-derived here.

Pure over a `list[Comment]` — the caller does the fetching. That keeps the forge round-trip at
the poll boundary and makes this testable without HTTP.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .forge.base import Comment

# Body conventions written by run_review (`ccr:fp=`) and the label-review skill (`ccr:label=`).
# Kept as loose scans, not anchored matches: both footers are embedded in prose/markdown
# (`<sub>ccr:fp=abc</sub>`, "ccr:label=wrong — 反证是 …").
_FP_RE = re.compile(r"ccr:fp=([A-Za-z0-9_-]+)")
_LABEL_RE = re.compile(r"ccr:label=([A-Za-z]+)")

# The verdict vocabulary the skill writes. Anything else is treated as unlabeled rather than
# accepted: a typo'd verdict must show up as still-pending, not silently pollute ground truth.
VERDICTS = ("important", "minor", "debatable", "wrong")


@dataclass
class Finding:
    """A published finding comment joined with its verdict reply (if any)."""
    fp: str
    comment: Comment
    label: str = ""            # "" = pending a verdict

    @property
    def pending(self) -> bool:
        return not self.label


def findings(comments: list[Comment]) -> list[Finding]:
    """Published findings on a PR/MR, each with its `ccr:label` verdict when one exists.

    A published finding is an ANCHORED comment (`thread_id` != "") carrying a `ccr:fp=`. The
    anchor requirement is what excludes the review summary note, which also lists `ccr:fp=`
    — once per fallback finding, in one un-anchored body. That note is correctly skipped: it
    has no thread, so nothing in it can be replied to, so nothing in it can be labeled. (That
    cost is why `run_review` degrades line → file anchor before ever falling back to it.)
    """
    verdicts: dict[str, str] = {}
    for c in comments:
        if not c.thread_id or not c.reply_to:       # a verdict is a REPLY in a finding's thread
            continue
        m = _LABEL_RE.search(c.body or "")
        if m and m.group(1) in VERDICTS:
            verdicts.setdefault(c.thread_id, m.group(1))   # first verdict wins; later ones are discussion
    out = []
    for c in comments:
        if not c.thread_id:
            continue
        m = _FP_RE.search(c.body or "")
        if m:
            out.append(Finding(fp=m.group(1), comment=c, label=verdicts.get(c.thread_id, "")))
    return out


def pending(comments: list[Comment]) -> list[Finding]:
    """Published findings still awaiting a verdict — the nudge's source of truth."""
    return [f for f in findings(comments) if f.pending]


def pending_key(found: list[Finding]) -> str:
    """Identity of a pending SET, for nudge decay (`context.base.Nudge`).

    Hashes each finding's stable fp PLUS its published comment id. The fp identifies the issue,
    while the id identifies this review round's occurrence: if the same issue is published again
    after an earlier round was labeled, it is new work and must reopen the nudge. Sorted so comment
    order (two forge surfaces, interleaved by time) can't churn the key and reset the decay.
    """
    identities = sorted(f"{f.fp}:{f.comment.id or f.comment.thread_id}" for f in found)
    return hashlib.sha256("\n".join(identities).encode()).hexdigest()[:12] if identities else ""
