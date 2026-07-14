"""GitLabForge — the GitLab adapter for the `Forge` port.

Maps GitLab's merge-request REST surface onto the neutral domain: `iid`→`number`,
`opened`→`open`, `/merge_requests` paths. All GitLab-specific shape lives here; callers
see only `PullRequest` / `Comment`. The recent-window *policy* is not here — it's
`base.build_window`, composed over `recent` + `get`.
"""
from __future__ import annotations

import urllib.parse

from ._rest import RestClient
from .base import Comment, Forge, ForgeError, MergeReadiness, PullRequest, Release

# GitLab persisted state → neutral.
_STATE_IN = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "closed"}

# GitLab `detailed_merge_status` → neutral MergeReadiness. Anything unlisted (checking,
# preparing, unchecked, need_rebase, broken_status, …) falls through to UNKNOWN — the safe
# value while GitLab is still computing, or for statuses devloop doesn't act on.
_READINESS_IN = {
    "mergeable": MergeReadiness.READY,
    "conflict": MergeReadiness.CONFLICT,
    "discussions_not_resolved": MergeReadiness.DISCUSSIONS_UNRESOLVED,
    "draft_status": MergeReadiness.DRAFT,
    "not_approved": MergeReadiness.NEEDS_APPROVAL,
    "ci_must_pass": MergeReadiness.CI_BLOCKED,
    "ci_still_running": MergeReadiness.CI_BLOCKED,
}


class GitLabForge(Forge):
    provider = "gitlab"

    def __init__(self, host: str, path: str, token: str, *, timeout: int = 10):
        enc = urllib.parse.quote(path, safe="")
        self.c = RestClient(
            f"https://{host}/api/v4/projects/{enc}",
            {"PRIVATE-TOKEN": token},
            timeout=timeout,
        )
        self._diff_refs_memo: dict[int, dict] = {}  # MR iid → diff_refs（同一轮 N 条 inline 共用）

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

    def default_branch(self) -> str:
        return (self.c.get("") or {}).get("default_branch") or ""   # GET /projects/{id}

    def merge_readiness(self, number: int) -> MergeReadiness:
        return self._readiness(self.c.get(f"merge_requests/{number}"))

    @staticmethod
    def _readiness(d: dict) -> MergeReadiness:
        """Map a raw MR dict to neutral readiness. Prefer the modern `detailed_merge_status`;
        fall back to the boolean `has_conflicts` (older GitLab, or an unmapped status). Anything
        else → UNKNOWN, which includes the async 'checking'/'unchecked' window."""
        status = _READINESS_IN.get(d.get("detailed_merge_status") or "")
        if status is not None:
            return status
        if d.get("has_conflicts"):
            return MergeReadiness.CONFLICT
        return MergeReadiness.UNKNOWN

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

    def close(self, number: int) -> PullRequest:
        return self._to_pr(self.c.put(f"merge_requests/{number}", {"state_event": "close"}))

    def _to_release(self, d: dict) -> Release:
        links = d.get("_links") or {}
        commit = d.get("commit") or {}
        return Release(
            tag=d.get("tag_name", ""),
            name=d.get("name") or d.get("tag_name", "") or "",
            target=commit.get("id", "") or "",   # GitLab resolves ref → the tagged commit sha
            web_url=links.get("self", "") or "",
            created_at=d.get("released_at") or d.get("created_at"),
        )

    def create_release(self, *, tag: str, target: str, name: str = "", notes: str = "") -> Release:
        # `ref` creates the tag server-side when it doesn't exist yet (branch name or sha).
        return self._to_release(self.c.post("releases", {
            "tag_name": tag,
            "ref": target,
            "name": name or tag,
            "description": notes,
        }))

    def latest_release(self) -> Release | None:
        # GitLab lists releases released_at-desc by default → first entry is the latest.
        out = self.c.get("releases", per_page=1)
        rels = out if isinstance(out, list) else []
        return self._to_release(rels[0]) if rels else None

    def comments(self, number: int) -> list[Comment]:
        # One endpoint covers both surfaces: /discussions returns plain notes (as
        # single-note, `individual_note` discussions) AND positioned ones — so unlike
        # GitHub this needs no second fetch. `system` notes are GitLab's activity log
        # ("changed the description"), not comments.
        out = self.c.get(f"merge_requests/{number}/discussions", per_page=50)
        discussions = out if isinstance(out, list) else []
        return [
            self._to_comment(d, n)
            for d in discussions for n in d.get("notes", [])
            if not n.get("system")
        ]

    @staticmethod
    def _to_comment(discussion: dict, note: dict) -> Comment:
        # An individual_note discussion wraps a plain note. GitLab would actually let us reply
        # into one (POST .../notes promotes it to a thread), but the port reports thread_id=""
        # anyway: GitHub CAN'T reply to its plain-comment surface, and a port whose reply
        # threads on one provider and raises on the other forces callers to branch on provider
        # — the exact thing this port exists to prevent. Findings are diff-anchored, so the
        # real reply path never lands here.
        threaded = not discussion.get("individual_note")
        notes = [n for n in discussion.get("notes", []) if not n.get("system")]
        root = notes[0] if notes else {}
        pos = note.get("position") or {}
        return Comment(
            author=(note.get("author") or {}).get("username", "?"),
            body=note.get("body") or "",
            id=str(note.get("id") or ""),
            thread_id=str(discussion.get("id") or "") if threaded else "",
            # GitLab has no per-note parent — every note in a discussion answers its root.
            reply_to=str(root.get("id") or "") if threaded and note.get("id") != root.get("id") else "",
            path=pos.get("new_path") or "",
            line=pos.get("new_line"),
        )

    def comment(self, number: int, body: str) -> None:
        self.c.post(f"merge_requests/{number}/notes", {"body": body})

    def reply(self, number: int, target: Comment, body: str) -> None:
        if not target.thread_id:
            raise ForgeError(f"MR !{number}: comment {target.id or '?'} is a plain note — "
                             "GitLab can only reply inside a discussion")
        self.c.post(f"merge_requests/{number}/discussions/{target.thread_id}/notes",
                    {"body": body})

    def diff_comment(self, number: int, body: str, path: str, line: int) -> None:
        # Positioned discussion — GitLab re-anchors it on every push and folds it as
        # "outdated" once the lines change; with the project setting
        # `resolve_outdated_diff_discussions` it even auto-resolves then.
        refs = self._diff_refs(number)
        self.c.post(f"merge_requests/{number}/discussions", {
            "body": body,
            "position": {
                "position_type": "text",
                "base_sha": refs.get("base_sha"),
                "start_sha": refs.get("start_sha"),
                "head_sha": refs.get("head_sha"),
                "new_path": path,
                "new_line": line,
            },
        })

    def _diff_refs(self, number: int) -> dict:
        """The MR's current diff version (base/start/head sha) a position anchors against.
        Memoized per MR: one review round posts N findings against the same diff."""
        if number not in self._diff_refs_memo:
            refs = self.c.get(f"merge_requests/{number}").get("diff_refs") or {}
            # A text position needs ALL THREE shas — a partial diff_refs would leak None
            # into the position and come back as an opaque HTTP 400. Fail early instead.
            missing = [k for k in ("base_sha", "start_sha", "head_sha") if not refs.get(k)]
            if missing:
                raise ForgeError(f"MR !{number} diff_refs missing {'/'.join(missing)} — "
                                 "cannot anchor a diff comment")
            self._diff_refs_memo[number] = refs
        return self._diff_refs_memo[number]
