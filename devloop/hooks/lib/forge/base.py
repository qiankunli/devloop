"""Forge domain — the provider-neutral core that the rest of devloop depends on.

The domain object is a *code-review proposal*, called `PullRequest` here regardless of
which forge backs it (GitHub PR / GitLab MR are the same concept under two names). This
module is PURE: dataclasses + the `Forge` port (ABC) + tiny helpers, no git / HTTP / config
imports — so both the adapters (which produce `PullRequest`s) and the state layer (which
persists them) depend inward on it, never the reverse.

Greenfield seam: the boundary is at *provider*, not *transport*. GitHub and GitLab are two
live adapters picked per-repo (see `__init__.forge_for_repo`), each implementing this port.
Naming is neutral on purpose — `number` (not GitLab's `iid`), state normalized to
`open|merged|closed` — so no call site needs to know which forge it's talking to. Per-forge
vocabulary (PR/MR, #/!) is reattached only at display time via `vocab()`.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

PRS_CAP = 5   # max entries in the recent-PR window the monitor tracks

# Provider → (noun, number-sigil) for display. The domain stores neutral values;
# this is the ONLY place the GitHub/GitLab vocabulary is reattached.
_VOCAB = {"github": ("PR", "#"), "gitlab": ("MR", "!")}


def vocab(provider: str | None) -> tuple[str, str]:
    """(noun, sigil) for a provider, e.g. ('PR', '#') / ('MR', '!'). Unknown → PR/#."""
    return _VOCAB.get(provider or "", ("PR", "#"))


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
    adapter (GitHub's open/closed + a `merged` flag collapses to these three). `provider`
    is carried through persistence so display can pick the right vocabulary after a reload.
    'inactive' (merged/closed) is derived, never stored.
    """
    number: int
    provider: str = ""          # "github" | "gitlab" — drives display vocabulary only
    title: str = ""
    state: str = ""             # neutral: "open" | "merged" | "closed"
    source_branch: str = ""
    target_branch: str = ""
    web_url: str = ""
    sha: str = ""               # head sha — the monitor uses it for the ancestry check
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

    @property
    def label(self) -> str:
        """Forge-flavored display label, e.g. 'PR #12' / 'MR !12'."""
        noun, sigil = vocab(self.provider)
        return f"{noun} {sigil}{self.number}"

    @classmethod
    def from_dict(cls, d: dict) -> "PullRequest":
        return cls(
            number=int(d["number"]),
            provider=d.get("provider", ""),
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
    author: str = ""
    body: str = ""


# ── the port ──────────────────────────────────────────────────────────────────
class Forge(abc.ABC):
    """What devloop needs from a code-review host — defined in the domain's terms, NOT
    mirroring any one SDK. GitLab/GitHub adapters implement this as peers.

    Methods return neutral `PullRequest` / `Comment`. `state` arguments/values are the
    neutral enum ('open'/'closed'/'all' for queries); each adapter translates to its own
    API vocabulary internally.
    """

    provider: str = ""

    @abc.abstractmethod
    def create(self, *, source_branch: str, target_branch: str, title: str,
               body: str = "") -> PullRequest: ...

    @abc.abstractmethod
    def get(self, number: int) -> PullRequest: ...

    @abc.abstractmethod
    def update(self, number: int, **fields) -> PullRequest: ...

    @abc.abstractmethod
    def list(self, *, state: str = "all", source_branch: str | None = None,
             per_page: int = 20) -> list[PullRequest]: ...

    @abc.abstractmethod
    def window(self, anchor: int | None) -> list[PullRequest]: ...

    @abc.abstractmethod
    def comments(self, number: int) -> list[Comment]: ...

    # Default: the most-recent PR whose source branch matches. Adapters may override.
    def for_branch(self, branch: str) -> PullRequest | None:
        prs = self.list(source_branch=branch, state="all", per_page=20)
        return prs[0] if prs else None


def _window_numbers(anchor: int | None, latest: int, cap: int) -> list[int]:
    """Pure number-math for a contiguous-numbering window (GitLab iids). Returns the
    target number set, newest-inclusive, with the anchor neighborhood always present.

    - anchor known: base `[anchor-2, latest]`; if ≤cap pad down to cap ending at latest,
      else keep {anchor-2,anchor-1,anchor} + the 2 newest (anchor always present).
    - anchor unknown: the latest `cap`.
    """
    if latest < 1:
        return []
    if anchor is None:
        lo = max(1, latest - (cap - 1))
        return list(range(lo, latest + 1))
    if latest <= anchor + (cap - 3):
        lo = max(1, latest - (cap - 1))
        lo = min(lo, max(1, anchor - 2))
        return list(range(lo, latest + 1))
    s = {anchor - 2, anchor - 1, anchor, latest - 1, latest}
    return sorted(i for i in s if i >= 1)
