"""Unified GitLab facade — the single, standardized surface for GitLab access.

    from lib.gitlab import GitLabClient, MergeRequests, to_mrrefs
    cl = GitLabClient.for_repo(repo_dir)      # None if no token / not GitLab
    if cl:
        mrs = MergeRequests(cl)
        mr = mrs.create(source_branch="feat/x", target_branch="master", title="...")

All transport lives in `client.py`; operations in `mr.py` mirror the python-gitlab
SDK and GitLab MCP tool names so the layer can be re-backed with minimal changes.
"""
from __future__ import annotations

from .client import (
    GitLabAuthError,
    GitLabClient,
    GitLabError,
    GitLabNotFound,
    Project,
    load_token,
    resolve_project,
)
from .mr import MergeRequests, to_mrrefs
