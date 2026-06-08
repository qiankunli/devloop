"""Unified forge facade — provider-neutral access to GitHub / GitLab, picked per-repo.

    from lib.forge import forge_for_repo, ForgeError
    f = forge_for_repo(repo_dir)              # None if no token / unsupported remote
    if f:
        pr = f.create(source_branch="feat/x", target_branch="main", title="...")

`forge_for_repo` parses the repo's `origin`, decides the provider from the host (config
can override the type for self-hosted / SSH-alias remotes), loads the right token, and
returns the matching adapter. The aggregate-workspace case is first-class: each repo
resolves its own provider, so a GitHub subproject and a GitLab one coexist.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import config, gitcmd
from .base import (
    Comment,
    Forge,
    ForgeAuthError,
    ForgeError,
    ForgeNotFound,
    PullRequest,
    vocab,
)
from .github import GitHubForge
from .gitlab import GitLabForge


def parse_origin(repo_dir: str | Path) -> tuple[str, str] | None:
    """`origin` remote URL → (host, path). None if unresolvable.

    Handles `git@host:group/proj.git` and `https://host/group/proj.git`. `path` keeps the
    full group/subgroup tail (GitLab) or `owner/repo` (GitHub).
    """
    r = gitcmd.git(repo_dir, "remote", "get-url", "origin")
    if not r.ok or not r.out:
        return None
    url = r.out
    if url.startswith("git@") or ("@" in url and "://" not in url):
        m = re.match(r"\w+@([^:]+):(.+?)(?:\.git)?$", url)
    else:
        m = re.match(r"https?://[^/]*?([^/@]+)/(.+?)(?:\.git)?$", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def detect_provider(host: str, explicit_type: str | None) -> str:
    """'github' | 'gitlab'. Config `type` wins (covers GHE / self-hosted GitLab on a
    custom host); otherwise infer from the host."""
    if explicit_type in ("github", "gitlab"):
        return explicit_type
    h = host.lower()
    if h == "github.com" or h.startswith("github."):
        return "github"
    return "gitlab"


def forge_for_repo(repo_dir: str | Path, *, timeout: int = 10) -> Forge | None:
    """Build the forge adapter for this repo's origin. None if the remote is unresolvable
    or no token is available (callers skip quietly — forge features are best-effort)."""
    parsed = parse_origin(repo_dir)
    if parsed is None:
        return None
    host, path = parsed
    entry = config.forge_entry(host, repo_dir)
    provider = detect_provider(host, entry.get("type"))
    token = config.forge_token(host, provider, repo_dir)
    if not token:
        return None
    api_host = entry.get("api_host") or host   # SSH-alias / mirror → real API host
    if provider == "github":
        owner, _, name = path.partition("/")
        if not owner or not name:
            return None
        return GitHubForge(api_host, owner, name, token, timeout=timeout)
    return GitLabForge(api_host, path, token, timeout=timeout)
