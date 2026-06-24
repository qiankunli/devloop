"""GitHubForge — the GitHub adapter for the `Forge` port.

Maps GitHub's pull-request REST surface onto the neutral domain: PR `number` (already
neutral), `/pulls` paths, `Authorization: Bearer` auth. GitHub's state model (open/closed +
a separate `merged`/`merged_at`) collapses to the neutral `open|merged|closed` here — that
normalization is exactly what this adapter exists for. The recent-window *policy* is not
here — it's `base.build_window`, composed over `recent` + `get`.
"""
from __future__ import annotations

from ._rest import RestClient
from .base import Comment, Forge, PullRequest


class GitHubForge(Forge):
    provider = "github"

    def __init__(self, host: str, owner: str, name: str, token: str, *, timeout: int = 10):
        # github.com → api.github.com; GitHub Enterprise → https://<host>/api/v3
        api = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
        self.owner, self.name = owner, name
        self.c = RestClient(
            f"{api}/repos/{owner}/{name}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )

    def _to_pr(self, d: dict) -> PullRequest:
        # `merged` is only on the single-PR response; list items carry `merged_at`.
        merged = bool(d.get("merged") or d.get("merged_at"))
        gh_state = d.get("state", "")
        state = "merged" if merged else ("open" if gh_state == "open" else "closed")
        return PullRequest(
            number=int(d["number"]),
            title=d.get("title", ""),
            state=state,
            source_branch=(d.get("head") or {}).get("ref", ""),
            target_branch=(d.get("base") or {}).get("ref", ""),
            web_url=d.get("html_url", ""),
            sha=(d.get("head") or {}).get("sha", "") or "",
            updated_at=d.get("updated_at"),
        )

    def _list(self, **params) -> list[PullRequest]:
        params.setdefault("state", "all")
        params.setdefault("sort", "created")
        params.setdefault("direction", "desc")
        out = self.c.get("pulls", **params)
        return [self._to_pr(d) for d in out] if isinstance(out, list) else []

    def prs_for_branch(self, branch: str) -> list[PullRequest]:
        # `head` filter is `owner:ref`; our branches are pushed to origin (same repo).
        return self._list(head=f"{self.owner}:{branch}", per_page=20)

    def recent(self, limit: int) -> list[PullRequest]:
        return self._list(per_page=limit)

    def get(self, number: int) -> PullRequest:
        return self._to_pr(self.c.get(f"pulls/{number}"))

    def description(self, number: int) -> str:
        return self.c.get(f"pulls/{number}").get("body") or ""

    def create(self, *, source_branch: str, target_branch: str, title: str,
               body: str = "") -> PullRequest:
        return self._to_pr(self.c.post("pulls", {
            "title": title,
            "head": source_branch,
            "base": target_branch,
            "body": body,
        }))

    def update(self, number: int, **fields) -> PullRequest:
        body = {}
        if "title" in fields:
            body["title"] = fields["title"]
        if "body" in fields:
            body["body"] = fields["body"]
        if "target_branch" in fields:
            body["base"] = fields["target_branch"]
        return self._to_pr(self.c.patch(f"pulls/{number}", body))

    def close(self, number: int) -> PullRequest:
        return self._to_pr(self.c.patch(f"pulls/{number}", {"state": "closed"}))

    def comments(self, number: int) -> list[Comment]:
        # PR conversation comments live on the issue endpoint (review comments are a
        # separate, line-anchored surface we don't surface here).
        out = self.c.get(f"issues/{number}/comments", per_page=50)
        notes = out if isinstance(out, list) else []
        return [
            Comment(author=(n.get("user") or {}).get("login", "?"), body=n.get("body") or "")
            for n in notes
        ]

    def comment(self, number: int, body: str) -> None:
        # Conversation comment on the PR (= issue comment), same surface `comments()` reads.
        self.c.post(f"issues/{number}/comments", {"body": body})
