"""GitLabClient — the single transport seam for ALL GitLab access in devloop.

Everything GitLab goes through here: token loading, origin→(host, project) resolution,
the one `request()` that speaks HTTP, error typing, timeouts. Scripts/monitor never
touch urllib directly — they call `mr.MergeRequests` over this client.

Why this shape: the operation surface (mr.py) mirrors python-gitlab
(`project.mergerequests.*`) and the GitLab MCP tools, so swapping the transport
later — to the python-gitlab SDK, or to a GitLab MCP server — means reimplementing
ONLY `request()` (and `load_token`/`resolve_project`); every call site stays put.

Auth: token from `~/.config/devloop/config.json` (`gitlab.token`), overridable by the
`GITLAB_TOKEN` env var. No token → `for_repo` returns None (callers skip quietly).
Host: derived from each repo's `origin` remote, overridable by `gitlab.host` in config.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .. import config, gitcmd

DEFAULT_TIMEOUT = 10


class GitLabError(Exception):
    """Any GitLab transport/API failure."""


class GitLabAuthError(GitLabError):
    """No usable token."""


class GitLabNotFound(GitLabError):
    """404 from the API."""


@dataclass(frozen=True)
class Project:
    host: str
    path: str   # e.g. "acme/widgets"

    @property
    def encoded(self) -> str:
        return urllib.parse.quote(self.path, safe="")

    @property
    def base(self) -> str:
        return f"https://{self.host}/api/v4/projects/{self.encoded}"


def load_token() -> str | None:
    """Token from config (`gitlab.token`), env `GITLAB_TOKEN` overriding. None if absent."""
    return config.gitlab_token()


def resolve_project(repo_dir: str | Path) -> Project | None:
    """Parse the `origin` remote URL → Project(host, path). None if unresolvable.

    `gitlab.host` in config overrides the host parsed from origin (for SSH host
    aliases / mirrors / non-standard remotes); the project path still comes from origin.
    """
    r = gitcmd.git(repo_dir, "remote", "get-url", "origin")
    if not r.ok or not r.out:
        return None
    url = r.out
    if url.startswith("git@"):
        m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    else:
        m = re.match(r"https?://[^/]*?([^/@]+)/(.+?)(?:\.git)?$", url)
    if not m:
        return None
    return Project(host=config.gitlab_host() or m.group(1), path=m.group(2))


class GitLabClient:
    """Thin HTTP client over the GitLab v4 REST API for one project."""

    def __init__(self, project: Project, token: str, *, timeout: int = DEFAULT_TIMEOUT):
        self.project = project
        self._token = token
        self.timeout = timeout

    @classmethod
    def for_repo(cls, repo_dir: str | Path, *, timeout: int = DEFAULT_TIMEOUT) -> "GitLabClient | None":
        """Build a client for the repo's origin. None if no token / not a GitLab remote
        (callers skip quietly — GitLab features are best-effort)."""
        token = load_token()
        if not token:
            return None
        project = resolve_project(repo_dir)
        if project is None:
            return None
        return cls(project, token, timeout=timeout)

    # ── the single transport method ───────────────────────────────────────────
    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None) -> Any:
        """`<base>/<path>` with PRIVATE-TOKEN auth. Returns parsed JSON.

        `params` values that are lists encode as repeated keys (GitLab `iids[]`).
        Raises GitLabNotFound (404) / GitLabError (other). This is the ONLY place
        that performs HTTP — swap it to the SDK/MCP and the rest is unchanged.
        """
        url = f"{self.project.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"PRIVATE-TOKEN": self._token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise GitLabNotFound(f"{method} {path} → 404") from e
            raise GitLabError(f"{method} {path} → HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise GitLabError(f"{method} {path} → {e}") from e

    # convenience verbs
    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: dict) -> Any:
        return self.request("POST", path, body=body)

    def put(self, path: str, body: dict) -> Any:
        return self.request("PUT", path, body=body)
