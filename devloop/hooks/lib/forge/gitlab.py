"""GitLabForge — the GitLab adapter for the `Forge` port.

Maps GitLab's merge-request REST surface onto the neutral domain: `iid`→`number`,
`opened`→`open`, `/merge_requests` paths. All GitLab-specific shape lives here; callers
see only `PullRequest` / `Comment`. The recent-window *policy* is not here — it's
`base.build_window`, composed over `recent` + `get`.
"""
from __future__ import annotations

import urllib.parse

from ._rest import RestClient
from .base import Comment, Forge, PullRequest

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
            title=d.get("title", ""),
            state=_STATE_IN.get(d.get("state", ""), d.get("state", "")),
            source_branch=d.get("source_branch", ""),
            target_branch=d.get("target_branch", ""),
            web_url=d.get("web_url", ""),
            sha=d.get("sha", "") or "",
            updated_at=d.get("updated_at"),
        )

    def _list(self, **params) -> list[PullRequest]:
        params.setdefault("order_by", "created_at")
        params.setdefault("sort", "desc")
        out = self.c.get("merge_requests", **params)
        return [self._to_pr(d) for d in out] if isinstance(out, list) else []

    def prs_for_branch(self, branch: str) -> list[PullRequest]:
        return self._list(source_branch=branch, state="all", per_page=20)

    def recent(self, limit: int) -> list[PullRequest]:
        return self._list(state="all", per_page=limit)

    def get(self, number: int) -> PullRequest:
        return self._to_pr(self.c.get(f"merge_requests/{number}"))

    def description(self, number: int) -> str:
        return self.c.get(f"merge_requests/{number}").get("description") or ""

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
