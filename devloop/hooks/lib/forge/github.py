"""GitHubForge — the GitHub adapter for the `Forge` port.

Maps GitHub's pull-request REST surface onto the neutral domain: PR `number` (already
neutral), `/pulls` paths, `Authorization: Bearer` auth, `#`-flavored vocabulary. GitHub's
state model (open/closed + a separate `merged`/`merged_at`) collapses to the neutral
`open|merged|closed` here — that normalization is exactly what this adapter exists for.
"""
from __future__ import annotations

from ._rest import RestClient
from .base import (
    PRS_CAP,
    Comment,
    Forge,
    ForgeNotFound,
    PullRequest,
)


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
            provider=self.provider,
            title=d.get("title", ""),
            state=state,
            source_branch=(d.get("head") or {}).get("ref", ""),
            target_branch=(d.get("base") or {}).get("ref", ""),
            web_url=d.get("html_url", ""),
            sha=(d.get("head") or {}).get("sha", "") or "",
            updated_at=d.get("updated_at"),
        )

    def list(self, *, state: str = "all", source_branch: str | None = None,
             per_page: int = 20) -> list[PullRequest]:
        # GitHub `state` is open/closed/all; neutral 'merged' has no query form (it's a
        # closed PR) → fall back to 'all' and let the caller filter on the normalized state.
        gh_state = state if state in ("open", "closed", "all") else "all"
        params: dict = {"state": gh_state, "per_page": per_page, "sort": "created",
                        "direction": "desc"}
        if source_branch:
            params["head"] = f"{self.owner}:{source_branch}"
        out = self.c.get("pulls", **params)
        return [self._to_pr(d) for d in out] if isinstance(out, list) else []

    def get(self, number: int) -> PullRequest:
        return self._to_pr(self.c.get(f"pulls/{number}"))

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

    def comments(self, number: int) -> list[Comment]:
        # PR conversation comments live on the issue endpoint (review comments are a
        # separate, line-anchored surface we don't surface here).
        out = self.c.get(f"issues/{number}/comments", per_page=50)
        notes = out if isinstance(out, list) else []
        return [
            Comment(author=(n.get("user") or {}).get("login", "?"), body=n.get("body") or "")
            for n in notes
        ]

    def window(self, anchor: int | None) -> list[PullRequest]:
        """Recent-PR window. GitHub numbers are shared with issues (non-contiguous as PRs),
        so the contiguous number-math doesn't apply — take the newest PRS_CAP and make sure
        the current branch's anchor PR is present (fetch it if it fell off the recent list)."""
        recent = self.list(state="all", per_page=PRS_CAP)
        by_num = {p.number: p for p in recent}
        if anchor and anchor not in by_num:
            try:
                by_num[anchor] = self.get(anchor)
            except ForgeNotFound:
                pass
        ordered = sorted(by_num.values(), key=lambda p: p.number, reverse=True)
        return ordered[:PRS_CAP]
