"""Unified forge facade — provider-neutral access to GitHub / GitLab, picked per-repo.

    from lib.forge import forge_for_repo
    from domain.forge import ForgeError, build_window
    f = forge_for_repo(repo_dir)              # None if no token / unsupported remote
    if f:
        pr = f.create(source_branch="feat/x", target_branch="main", title="...")

`forge_for_repo` resolves the repo's origin → (provider, host, token) in one place
(`resolve_forge`), then builds the matching adapter. The aggregate-workspace case is
first-class: each repo resolves its own provider, so a GitHub subproject and a GitLab one
coexist. Cross-provider window *policy* is `build_window` (domain-level), not per-adapter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import config, gitcmd
from domain.forge import (
    Comment,
    Forge,
    ForgeAuthError,
    ForgeError,
    ForgeNotFound,
    MergeReadiness,
    PullRequest,
    Release,
    build_window,
    parse_pr_number,
    pr_label,
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


@dataclass(frozen=True)
class ForgeResolution:
    """The repo's forge identity, resolved ONCE from the three sources (origin remote /
    config / env). `token` is None when no usable credential exists."""
    provider: str
    host: str        # host parsed from origin (the config key)
    api_host: str    # host to hit for the API (config `api_host` override, else `host`)
    path: str        # owner/repo (GitHub) or group/subgroup/proj (GitLab)
    token: str | None


def resolve_forge(repo_dir: str | Path) -> ForgeResolution | None:
    """origin + config + env → one ForgeResolution. None if origin is unresolvable.

    Single place the three sources are combined, so call sites never re-derive provider /
    host / token piecemeal.
    """
    parsed = parse_origin(repo_dir)
    if parsed is None:
        return None
    host, path = parsed
    entry = config.forge_entry(host, repo_dir)
    provider = detect_provider(host, entry.get("type"))
    return ForgeResolution(
        provider=provider,
        host=host,
        api_host=entry.get("api_host") or host,
        path=path,
        token=config.forge_token(host, provider, repo_dir),
    )


def forge_for_repo(repo_dir: str | Path, *, timeout: int = 10) -> Forge | None:
    """Build the forge adapter for this repo's origin. None if the remote is unresolvable
    or no token is available (callers skip quietly — forge features are best-effort)."""
    r = resolve_forge(repo_dir)
    if r is None or not r.token:
        return None
    if r.provider == "github":
        owner, _, name = r.path.partition("/")
        if not owner or not name:
            return None
        return GitHubForge(r.api_host, owner, name, r.token, timeout=timeout)
    return GitLabForge(r.api_host, r.path, r.token, timeout=timeout)
