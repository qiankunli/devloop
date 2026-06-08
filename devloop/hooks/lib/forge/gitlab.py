"""GitLabForge — the GitLab adapter for the `Forge` port.

Maps GitLab's merge-request REST surface onto the neutral domain: `iid`→`number`,
`opened`→`open`, `/merge_requests` paths, `!`-flavored vocabulary. All GitLab-specific
shape lives here; callers see only `PullRequest` / `Comment`.
"""
from __future__ import annotations

import urllib.parse

from ._rest import RestClient
from .base import (
    PRS_CAP,
    Comment,
    Forge,
    PullRequest,
    _window_numbers,
)

# Neutral query-state → GitLab's vocabulary.
_STATE_OUT = {"all": "all", "open": "opened", "closed": "closed", "merged": "merged"}
# GitLab persisted state → neutral.
_STATE_IN = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "closed"}


class GitLabForge(Forge):
    provider = "gitlab"

    def __init__(self, host: str, path: str, token: str, *, timeout: int = 10):
        enc = urllib.parse.quote(path, safe="")
        self.c = RestClient(
            f"https://{host}/api/v4/projects/{enc}",
            {"PRIVATE-TOKEN": token},
            timeout=timeout,
        )

    def _to_pr(self, d: dict) -> PullRequest:
        return PullRequest(
            number=int(d["iid"]),
            provider=self.provider,
            title=d.get("title", ""),
            state=_STATE_IN.get(d.get("state", ""), d.get("state", "")),
            source_branch=d.get("source_branch", ""),
            target_branch=d.get("target_branch", ""),
            web_url=d.get("web_url", ""),
            sha=d.get("sha", "") or "",
            updated_at=d.get("updated_at"),
        )

    def list(self, *, state: str = "all", source_branch: str | None = None,
             per_page: int = 20) -> list[PullRequest]:
        params: dict = {"state": _STATE_OUT.get(state, "all"), "order_by": "created_at",
                        "sort": "desc", "per_page": per_page}
        if source_branch:
            params["source_branch"] = source_branch
        out = self.c.get("merge_requests", **params)
        return [self._to_pr(d) for d in out] if isinstance(out, list) else []

    def get(self, number: int) -> PullRequest:
        return self._to_pr(self.c.get(f"merge_requests/{number}"))

    def create(self, *, source_branch: str, target_branch: str, title: str,
               body: str = "") -> PullRequest:
        return self._to_pr(self.c.post("merge_requests", {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": body,
            "remove_source_branch": True,
            "squash": False,
        }))

    def update(self, number: int, **fields) -> PullRequest:
        body = {}
        if "title" in fields:
            body["title"] = fields["title"]
        if "body" in fields:
            body["description"] = fields["body"]
        if "target_branch" in fields:
            body["target_branch"] = fields["target_branch"]
        return self._to_pr(self.c.put(f"merge_requests/{number}", body))

    def comments(self, number: int) -> list[Comment]:
        out = self.c.get(f"merge_requests/{number}/discussions", per_page=50)
        discussions = out if isinstance(out, list) else []
        return [
            Comment(author=(n.get("author") or {}).get("username", "?"), body=n.get("body") or "")
            for d in discussions for n in d.get("notes", [])
            if not n.get("system")
        ]

    def window(self, anchor: int | None) -> list[PullRequest]:
        """Recent-MR window. GitLab iids are contiguous per project, so the anchor
        neighborhood is cheap to fetch by `iids[]` — use the shared number-math."""
        newest = self.c.get("merge_requests", order_by="created_at", sort="desc", per_page=1)
        latest = int(newest[0]["iid"]) if isinstance(newest, list) and newest else None
        if latest is None:
            return []
        target = _window_numbers(anchor, latest, PRS_CAP)
        if not target:
            return []
        out = self.c.get("merge_requests", state="all", per_page=len(target) + 5,
                         **{"iids[]": list(target)})
        prs = [self._to_pr(d) for d in out] if isinstance(out, list) else []
        return sorted(prs, key=lambda p: p.number, reverse=True)
