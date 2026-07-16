"""GitHubForge — the GitHub adapter for the `Forge` port.

Maps GitHub's pull-request REST surface onto the neutral domain: PR `number` (already
neutral), `/pulls` paths, `Authorization: Bearer` auth. GitHub's state model (open/closed +
a separate `merged`/`merged_at`) collapses to the neutral `open|merged|closed` here — that
normalization is exactly what this adapter exists for. The recent-window *policy* is not
here — it's `base.build_window`, composed over `recent` + `get`.
"""
from __future__ import annotations

from ._rest import RestClient
from .base import Comment, Forge, ForgeError, ForgeNotFound, PullRequest, Release


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
        self._head_sha_memo: dict[int, str] = {}  # PR number → head sha（同一轮 N 条 inline 共用）

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

    def default_branch(self) -> str:
        return (self.c.get("") or {}).get("default_branch") or ""   # GET /repos/{owner}/{name}

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

    def _to_release(self, d: dict) -> Release:
        return Release(
            tag=d.get("tag_name", ""),
            name=d.get("name") or d.get("tag_name", "") or "",
            target=d.get("target_commitish", "") or "",
            web_url=d.get("html_url", ""),
            created_at=d.get("published_at") or d.get("created_at"),
        )

    def create_release(self, *, tag: str, target: str, name: str = "", notes: str = "") -> Release:
        return self._to_release(self.c.post("releases", {
            "tag_name": tag,
            "target_commitish": target,
            "name": name or tag,
            "body": notes,
        }))

    def latest_release(self) -> Release | None:
        # /releases/latest is the newest full release (excludes drafts + prereleases) — the
        # right baseline for an increment check. 404 = no releases yet → the first release.
        try:
            return self._to_release(self.c.get("releases/latest"))
        except ForgeNotFound:
            return None

    def comments(self, number: int) -> list[Comment]:
        # GitHub splits a PR's comments across two endpoints — conversation comments live on
        # the ISSUE surface, the line-anchored ones `diff_comment` writes on the PULLS surface.
        # Both are fetched and merged: a caller looking for what review posted can't be asked
        # to know which surface it landed on. Interleaved by creation time so the merged list
        # reads as one conversation.
        issue = self.c.get_all(f"issues/{number}/comments")
        review = self.c.get_all(f"pulls/{number}/comments")
        rows = [(n, False) for n in issue]
        rows += [(n, True) for n in review]
        rows.sort(key=lambda r: r[0].get("created_at") or "")   # ISO-8601 Z → lexical == chronological
        return [self._to_comment(n, anchored=a) for n, a in rows]

    @staticmethod
    def _to_comment(n: dict, *, anchored: bool) -> Comment:
        cid = str(n.get("id") or "")
        parent = str(n.get("in_reply_to_id") or "")
        return Comment(
            author=(n.get("user") or {}).get("login", "?"),
            body=n.get("body") or "",
            id=cid,
            # Only review comments thread; a thread is keyed by its ROOT comment id, which is
            # also what the replies endpoint takes as its target.
            thread_id=(parent or cid) if anchored else "",
            reply_to=parent,
            path=(n.get("path") or "") if anchored else "",
            # `line` goes null once a push makes the anchor outdated — `original_line` still
            # says where it was written, which is what a reader needs.
            line=(n.get("line") or n.get("original_line")) if anchored else None,
        )

    def comment(self, number: int, body: str) -> None:
        # Conversation comment on the PR (= issue comment), one of the two surfaces
        # `comments()` reads.
        self.c.post(f"issues/{number}/comments", {"body": body})

    def reply(self, number: int, target: Comment, body: str) -> None:
        if not target.thread_id:
            raise ForgeError(f"PR #{number}: comment {target.id or '?'} is a conversation "
                             "comment — GitHub can only reply to review comments")
        # thread_id is the root review comment id — exactly what /replies anchors to.
        self.c.post(f"pulls/{number}/comments/{target.thread_id}/replies", {"body": body})

    def diff_comment(self, number: int, body: str, path: str, line: int | None = None) -> None:
        # Anchored review comment — GitHub collapses it as outdated once a later push
        # changes the anchored lines. Needs the PR's current head sha as commit_id;
        # memoized per PR (one review round posts N findings against the same head).
        if number not in self._head_sha_memo:
            sha = (self.c.get(f"pulls/{number}").get("head") or {}).get("sha") or ""
            if not sha:
                raise ForgeError(f"PR #{number} has no head sha — cannot anchor a diff comment")
            self._head_sha_memo[number] = sha
        req = {"body": body, "commit_id": self._head_sha_memo[number], "path": path}
        if line is None:
            # File-level. `line`/`side` are omitted rather than nulled: the docs only say
            # line is "required unless using subject_type:file", so a null is untested
            # ground — sending no key is the shape the documented contract describes.
            req["subject_type"] = "file"
        else:
            req |= {"line": line, "side": "RIGHT"}
        self.c.post(f"pulls/{number}/comments", req)
