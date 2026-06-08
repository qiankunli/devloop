"""Merge-request operations over a GitLabClient.

Method names mirror python-gitlab (`project.mergerequests.list/get/create/update`)
and the GitLab MCP tool surface (create/get/update/list MR, discussions) so this
facade is portable: a future swap to the SDK or an MCP server reimplements these
against the new client without changing callers.

GitLab MR JSON keys (iid/title/state/source_branch/target_branch/web_url/updated_at)
line up 1:1 with `context.MRRef`, so `to_mrrefs` is a trivial map.
"""
from __future__ import annotations

from ..context import MRRef
from .client import GitLabClient

MRS_WINDOW_CAP = 5   # see plan §4: anchor window [iid-2, latest], capped


class MergeRequests:
    def __init__(self, client: GitLabClient):
        self.c = client

    # ── core CRUD (python-gitlab / MCP shaped) ─────────────────────────────────
    def list(self, *, state: str = "all", source_branch: str | None = None,
             order_by: str = "created_at", sort: str = "desc",
             per_page: int = 20, iids: list[int] | None = None) -> list[dict]:
        params: dict = {"state": state, "order_by": order_by, "sort": sort, "per_page": per_page}
        if source_branch:
            params["source_branch"] = source_branch
        if iids:
            params["iids[]"] = list(iids)
        out = self.c.get("merge_requests", **params)
        return out if isinstance(out, list) else []

    def get(self, iid: int) -> dict:
        return self.c.get(f"merge_requests/{iid}")

    def create(self, *, source_branch: str, target_branch: str, title: str,
               description: str = "", remove_source_branch: bool = True,
               squash: bool = False) -> dict:
        return self.c.post("merge_requests", {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": remove_source_branch,
            "squash": squash,
        })

    def update(self, iid: int, **fields) -> dict:
        return self.c.put(f"merge_requests/{iid}", fields)

    def discussions(self, iid: int) -> list[dict]:
        out = self.c.get(f"merge_requests/{iid}/discussions", per_page=50)
        return out if isinstance(out, list) else []

    # ── derived helpers ────────────────────────────────────────────────────────
    def latest_iid(self) -> int | None:
        """Highest (newest) MR iid in the project — iids are creation-ordered."""
        newest = self.list(order_by="created_at", sort="desc", per_page=1)
        return int(newest[0]["iid"]) if newest else None

    def for_branch(self, branch: str) -> dict | None:
        """Most-recent MR with this source branch, or None."""
        mrs = self.list(source_branch=branch, state="all", per_page=20)
        return mrs[0] if mrs else None

    def window(self, anchor_iid: int | None) -> list[dict]:
        """The recent-MR window for the repo (plan §4): whole-project, cap 5,
        anchored on the current branch's MR `anchor_iid`.

        - anchor known: base `[anchor-2, latest]`. If that's ≤5 (anchor near
          latest) pad the window to 5 ending at latest; if >5, keep the anchor
          neighborhood {N-2,N-1,N} + the 2 newest (so the anchor is always present).
        - anchor unknown: the latest 5.
        Returned sorted by iid desc (newest first).
        """
        latest = self.latest_iid()
        if latest is None:
            return []
        target = _window_iids(anchor_iid, latest, MRS_WINDOW_CAP)
        if not target:
            return []
        mrs = self.list(iids=target, state="all", per_page=len(target) + 5)
        return sorted(mrs, key=lambda m: int(m.get("iid", 0)), reverse=True)


def _window_iids(anchor: int | None, latest: int, cap: int) -> list[int]:
    """Pure iid-math for `window` (unit-testable). Returns the target iid set."""
    if latest < 1:
        return []
    if anchor is None:
        lo = max(1, latest - (cap - 1))
        return list(range(lo, latest + 1))
    if latest <= anchor + (cap - 3):          # contiguous fits (anchor near latest)
        lo = max(1, latest - (cap - 1))        # pad down to `cap`, ending at latest
        lo = min(lo, max(1, anchor - 2))       # but never drop the anchor neighborhood
        return list(range(lo, latest + 1))
    # window would exceed cap: anchor neighborhood {N-2,N-1,N} + 2 newest
    s = {anchor - 2, anchor - 1, anchor, latest - 1, latest}
    return sorted(i for i in s if i >= 1)


def to_mrrefs(dicts: list[dict]) -> list[MRRef]:
    """Map GitLab MR JSON → MRRef (keys line up 1:1)."""
    return [MRRef.from_dict(d) for d in dicts if d.get("iid") is not None]
