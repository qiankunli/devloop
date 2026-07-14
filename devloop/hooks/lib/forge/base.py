"""Forge domain — the provider-neutral core that the rest of devloop depends on.

The domain object is a *code-review proposal*, called `PullRequest` here regardless of
which forge backs it (GitHub PR / GitLab MR are the same concept under two names). This
module is PURE: dataclasses + the `Forge` port (ABC) + small domain helpers, no git / HTTP
/ config imports — so both the adapters (which produce `PullRequest`s) and the state layer
(which persists them) depend inward on it, never the reverse.

Greenfield seams:
- The boundary is at *provider*, not *transport*. GitHub and GitLab are two live adapters
  picked per-repo (see `__init__.forge_for_repo`), each implementing this port.
- The port exposes only the *fetch/mutate primitives* devloop needs; cross-provider
  *policy* (the recent-PR window) lives once in `build_window`, composed over those
  primitives — not duplicated in each adapter.
- `provider` is a property of the repo/forge, not of each PR. So `PullRequest` carries no
  provider; display vocabulary is reattached at render time via `pr_label` / `vocab` using
  the repo-level provider.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from enum import Enum

PRS_CAP = 5   # max entries in the recent-PR window the monitor tracks

# Provider → (noun, number-sigil) for display. The domain stores neutral values;
# this is the ONLY place the GitHub/GitLab vocabulary is reattached.
_VOCAB = {"github": ("PR", "#"), "gitlab": ("MR", "!")}


def vocab(provider: str | None) -> tuple[str, str]:
    """(noun, sigil) for a provider, e.g. ('PR', '#') / ('MR', '!'). Unknown → PR/#."""
    return _VOCAB.get(provider or "", ("PR", "#"))


def pr_label(provider: str | None, number: int) -> str:
    """Forge-flavored display label, e.g. 'PR #12' / 'MR !12'. Provider is repo-level."""
    noun, sigil = vocab(provider)
    return f"{noun} {sigil}{number}"


def parse_pr_number(s: str) -> int | None:
    """Extract a PR/MR number from a URL or a bare ref. Knows both forges' URL shapes
    (GitHub `/pull/N`, GitLab `/merge_requests/N`) so callers (CLI scripts) don't carry
    provider URL knowledge. Accepts a bare `N`, `#N`, or `!N` too."""
    m = (re.search(r"/(?:pull|merge_requests)/(\d+)", s) or re.fullmatch(r"[!#]?(\d+)", s.strip()))
    return int(m.group(1)) if m else None


# ── errors (provider-neutral; adapters raise these, call sites catch them) ─────
class ForgeError(Exception):
    """Any forge transport/API failure."""


class ForgeAuthError(ForgeError):
    """No usable token / 401 / 403."""


class ForgeNotFound(ForgeError):
    """404 from the API."""


# ── domain model ──────────────────────────────────────────────────────────────
@dataclass
class PullRequest:
    """A code-review proposal as devloop tracks it — neutral across forges.

    `number` is the per-repo sequential id (the number in the URL: GitLab `iid`,
    GitHub PR number). `state` is normalized to 'open' | 'merged' | 'closed' by the
    adapter (GitHub's open/closed + a `merged` flag collapses to these three). No
    `provider` field — that's a repo-level fact (see module docstring); display labels
    are built with `pr_label(provider, number)` at render time. 'inactive' (merged/closed)
    is derived, never stored.
    """
    number: int
    title: str = ""
    state: str = ""             # neutral: "open" | "merged" | "closed"
    source_branch: str = ""
    target_branch: str = ""
    web_url: str = ""
    sha: str = ""               # source-branch tip the monitor ancestry-checks against HEAD
    updated_at: str | None = None

    @property
    def inactive(self) -> bool:
        return self.state in ("merged", "closed")

    @property
    def is_open(self) -> bool:
        """In-flight: exists and still awaiting human merge ('open').

        The loop's 'create PR → human merges (out of the AI's hands) → next round'
        transition leaves the branch here. It's the fourth branch state beyond
        healthy/protected/inactive, and the one new work must NOT be stacked onto by default.
        """
        return self.state == "open"

    @classmethod
    def from_dict(cls, d: dict) -> "PullRequest":
        return cls(
            number=int(d["number"]),
            title=d.get("title", ""),
            state=d.get("state", ""),
            source_branch=d.get("source_branch", ""),
            target_branch=d.get("target_branch", ""),
            web_url=d.get("web_url", ""),
            sha=d.get("sha", ""),
            updated_at=d.get("updated_at"),
        )


@dataclass
class Comment:
    """A comment on a PR/MR — neutral across forges, and across both comment surfaces
    (plain conversation note + diff-anchored note; see `Forge.comments`).

    `id` / `thread_id` are OPAQUE: each adapter picks values its own `reply` understands
    (GitLab discussion id, GitHub top-level review-comment id), so callers never branch on
    provider. Grouping into threads is the caller's job — `thread_id` equality is the only
    contract, deliberately instead of a nested tree type (no caller needs one yet).

    Carries no label/finding vocabulary: a `ccr:fp=` / `ccr:label=` footer is just body text
    the review layer writes and greps. The forge is the join's source of truth, which is why
    nothing here is persisted locally — see `comments()`.
    """
    author: str = ""
    body: str = ""
    id: str = ""                # opaque, adapter-scoped; "" when the adapter can't supply one
    thread_id: str = ""         # same value for every comment in one thread; "" = standalone
    reply_to: str = ""          # `id` of the comment this replies to; "" = thread root
    path: str = ""              # diff anchor (new side); "" for plain conversation comments
    line: int | None = None


@dataclass
class Release:
    """A published release as devloop tracks it — neutral across forges.

    `tag` is the git tag the release points to; `target` is the commit-ish the tag was cut
    at (a branch name or sha). Like `PullRequest` it carries no provider — the tag is created
    server-side by the forge, so a release never requires a local `git push --tags`.
    """
    tag: str
    name: str = ""
    target: str = ""           # target_commitish (GitHub) / commit the tag resolved to (GitLab)
    web_url: str = ""
    created_at: str | None = None


class MergeReadiness(str, Enum):
    """Why a PR/MR can't be merged yet — the neutral form of GitLab's `detailed_merge_status`
    / GitHub's `mergeable_state`. Forges surface one blocking reason at a time; plus READY and
    UNKNOWN.

    UNKNOWN is the *safe* value, and first-class on purpose: forges compute mergeability
    ASYNCHRONOUSLY, so a just-pushed PR reads as "still checking" — that must never collapse to
    READY or to a real blocker.

    Fetched on demand via `Forge.merge_readiness` (a primitive, like `description`/`comments`),
    deliberately NOT a `PullRequest` field: it's a derived verdict over the source×target tips
    that goes stale the moment either branch moves (a merge into target can block YOUR PR with
    your branch unchanged), so it must not be snapshotted into the persisted, injected PR window.
    """
    READY = "ready"
    CONFLICT = "conflict"
    DISCUSSIONS_UNRESOLVED = "discussions_unresolved"
    CI_BLOCKED = "ci_blocked"
    NEEDS_APPROVAL = "needs_approval"
    DRAFT = "draft"
    UNKNOWN = "unknown"

    @property
    def blocks_merge(self) -> bool:
        """An ACTIONABLE blocker the author must clear — conflict / unresolved discussions / CI —
        as opposed to READY, a non-actionable wait (NEEDS_APPROVAL / DRAFT), or the async UNKNOWN.
        The shared predicate the surfaces (turn banner, wake channel) alert on, so 'what's worth
        nagging about' is defined once here, not re-listed per surface."""
        return self in {
            MergeReadiness.CONFLICT,
            MergeReadiness.DISCUSSIONS_UNRESOLVED,
            MergeReadiness.CI_BLOCKED,
        }


# ── the port ──────────────────────────────────────────────────────────────────
class Forge(abc.ABC):
    """What devloop needs from a code-review host — defined in the domain's terms, NOT
    mirroring any one SDK/REST surface. GitLab/GitHub adapters implement this as peers.

    Only fetch/mutate *primitives* live here; the recent-PR window *policy* is
    `build_window`, composed over `recent` + `get` so it's identical across forges.
    Methods return neutral `PullRequest` / `Comment`.
    """

    provider: str = ""

    @abc.abstractmethod
    def create(self, *, source_branch: str, target_branch: str, title: str,
               body: str = "") -> PullRequest: ...

    @abc.abstractmethod
    def get(self, number: int) -> PullRequest: ...

    @abc.abstractmethod
    def description(self, number: int) -> str:
        """The PR/MR body text. A separate primitive rather than a `PullRequest` field:
        bodies can be large and the PR window is persisted + injected, so they're fetched
        only at the moment a caller syncs the description (see smart_git_ops)."""

    @abc.abstractmethod
    def update(self, number: int, **fields) -> PullRequest: ...

    @abc.abstractmethod
    def close(self, number: int) -> PullRequest:
        """Close the PR/MR without merging. A distinct primitive, not `update(state=...)`: the
        two forges spell it incompatibly (GitLab `state_event=close`, GitHub `state=closed`), so
        the neutral verb hides that split instead of leaking either spelling into callers."""

    @abc.abstractmethod
    def prs_for_branch(self, branch: str) -> list[PullRequest]:
        """All PRs whose source is `branch`, newest first (an old finished one + a new
        open one can coexist after a branch is reused — callers pick)."""

    @abc.abstractmethod
    def recent(self, limit: int) -> list[PullRequest]:
        """The `limit` most-recently-created PRs in the repo, newest first."""

    @abc.abstractmethod
    def default_branch(self) -> str:
        """The repo's **forge-configured** default branch, e.g. 'main' / 'master' (one
        repo-level GET). This is the remote source of truth, fresher and more reliable than
        the local `refs/remotes/origin/HEAD` cache (which `git fetch` never updates). It is
        NOT necessarily the branch a team merges into — a repo may default to `main` yet treat
        `release` as trunk — so callers still layer a per-repo config override on top."""

    @abc.abstractmethod
    def create_release(self, *, tag: str, target: str, name: str = "", notes: str = "") -> Release:
        """Publish a release `name` at `tag`, creating the tag at `target` (a branch name or
        sha) SERVER-SIDE — no local `git push --tags`, so this needs no working tree and trips
        no push guard. GitHub POST /releases (`target_commitish`), GitLab POST /releases (`ref`).
        A write primitive; version/increment policy lives in the release orchestrator, not here."""

    @abc.abstractmethod
    def latest_release(self) -> "Release | None":
        """The most recent published release, or None when the repo has none yet (its first
        release). Read primitive — the orchestrator uses it to check the new version is an
        increment and to bound the 'changes since last release' notes draft."""

    @abc.abstractmethod
    def comments(self, number: int) -> list[Comment]:
        """Every human-visible comment on PR/MR `number` — BOTH surfaces: plain conversation
        notes and the diff-anchored ones `diff_comment` writes. The union is the point: a
        caller that wants to find what review posted must not have to know which surface it
        landed on (GitHub splits them across two endpoints; GitLab merges them into one).
        System/bot activity notes are excluded — they aren't comments anyone wrote.

        Carries `id`/`thread_id`, so a caller that reads then replies gets the reply target
        from this same call. That's why devloop persists no local comment refs: the forge is
        the durable store, and a ref cached elsewhere would only be a staler copy of this.
        """

    @abc.abstractmethod
    def comment(self, number: int, body: str) -> None:
        """Post a new comment on PR/MR `number` (GitHub issue comment / GitLab MR note).
        Write primitive — used to attach code-review history to the MR."""

    def diff_comment(self, number: int, body: str, path: str, line: int | None = None) -> None:
        """Post a comment anchored to `path:line` on the NEW side of PR/MR `number`'s diff,
        or to `path` as a whole when `line` is None. The anchor is what buys the forge's
        native comment lifecycle: when a later push changes those lines, the forge marks the
        comment outdated and folds it — so each review round's findings age out with the code
        instead of piling up as plain notes.

        `line=None` is a granularity knob on ONE surface, not a second surface — same endpoint,
        same lifecycle, same failure mode, one field dropped (GitHub `subject_type=file`,
        GitLab `position_type=file`). That's why it's a parameter here, whereas `comment` stays
        a separate method: that one is a different endpoint with a different lifecycle.
        File-level anchoring is the rung between a line anchor and the plain summary note —
        it still gets an id and a thread, so a finding posted this way is still replyable.

        Raises ForgeError when the anchor can't land (the line isn't in the current diff; the
        file isn't in it either; GitLab older than 16.4 has no file position type). Callers are
        expected to degrade — line → file → summary note. Concrete-with-default (peer of
        merge_readiness): an adapter with no anchored surface at all raises too."""
        raise ForgeError(f"{self.provider or 'forge'}: diff comments not supported")

    def reply(self, number: int, target: Comment, body: str) -> None:
        """Post `body` into `target`'s thread on PR/MR `number`. `target` must have come from
        THIS forge's `comments()` — its `id`/`thread_id` are adapter-private values.

        Raises ForgeError when `target` isn't threadable (`thread_id` == ""), rather than
        silently degrading to a standalone comment: a reply that lands detached from what it
        answers is a worse outcome than an error the caller can handle. Only the diff-anchored
        surface threads — uniformly across forges by construction, see `GitLabForge._to_comment`.
        Same raise-and-let-the-caller-degrade contract as `diff_comment`.
        """
        raise ForgeError(f"{self.provider or 'forge'}: replies not supported")

    def merge_readiness(self, number: int) -> MergeReadiness:
        """Why MR/PR `number` can't merge yet (CONFLICT / DISCUSSIONS_UNRESOLVED / …), or READY.
        A fetch primitive (peer of `description`), NOT a `PullRequest` field — see `MergeReadiness`.
        Concrete-with-default rather than abstract: an adapter that hasn't implemented it (today:
        GitHub) inherits the safe UNKNOWN instead of being forced to lie; GitLab overrides."""
        return MergeReadiness.UNKNOWN


def build_window(forge: Forge, anchor: int | None, cap: int = PRS_CAP) -> list[PullRequest]:
    """The recent-PR window: newest `cap` PRs, with the current branch's `anchor` PR always
    present (fetched if it fell off the recent list). Provider-agnostic policy composed over
    the port's `recent` + `get` primitives — one definition for every forge, regardless of
    whether its numbering is contiguous.
    """
    by_num = {p.number: p for p in forge.recent(cap)}
    if anchor is not None and anchor not in by_num:
        try:
            by_num[anchor] = forge.get(anchor)
        except ForgeNotFound:
            pass
    ordered = sorted(by_num.values(), key=lambda p: p.number, reverse=True)
    if anchor is not None and anchor in by_num and by_num[anchor] not in ordered[:cap]:
        # anchor older than the newest `cap` — keep newest cap-1 + the anchor.
        keep = [p for p in ordered if p.number != anchor][:cap - 1] + [by_num[anchor]]
        ordered = sorted(keep, key=lambda p: p.number, reverse=True)
    return ordered[:cap]
