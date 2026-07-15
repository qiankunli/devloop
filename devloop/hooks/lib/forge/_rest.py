"""RestClient — the single HTTP transport shared by every forge adapter.

The GitHub and GitLab REST surfaces differ only in base URL, auth header, and JSON
shapes; the request mechanics (urllib, params encoding, JSON body, error typing,
timeouts) are identical. So this is the ONE place that touches urllib — swap it for the
SDK / an MCP server later and the adapters stay put. Adapters parametrize it with their
base URL + headers and speak verbs (`get/post/put/patch`); they never import urllib.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import ForgeAuthError, ForgeError, ForgeNotFound

DEFAULT_TIMEOUT = 10


class RestClient:
    def __init__(self, base_url: str, headers: dict[str, str], *, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self._headers = dict(headers)
        self.timeout = timeout

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None) -> Any:
        """`<base_url>/<path>`, returns parsed JSON (None on empty body).

        `params` list values encode as repeated keys (e.g. GitLab `iids[]`).
        Maps 401/403 → ForgeAuthError, 404 → ForgeNotFound, else ForgeError. The
        ONLY HTTP call in the forge layer.
        """
        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url  # "" → repo root, no trailing slash
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = dict(self._headers)
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ForgeAuthError(f"{method} {path} → HTTP {e.code}") from e
            if e.code == 404:
                raise ForgeNotFound(f"{method} {path} → 404") from e
            raise ForgeError(f"{method} {path} → HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise ForgeError(f"{method} {path} → {e}") from e

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def get_all(self, path: str, *, per_page: int = 100, **params) -> list:
        """Fetch every page from a list endpoint using the page/per_page convention shared
        by GitHub and GitLab. Keeping the loop here makes "all" a transport guarantee instead
        of an adapter promise that silently stops at its first page."""
        out = []
        page = 1
        while True:
            batch = self.get(path, **params, page=page, per_page=per_page)
            if not isinstance(batch, list):
                return out
            out.extend(batch)
            if len(batch) < per_page:
                return out
            page += 1

    def post(self, path: str, body: dict) -> Any:
        return self.request("POST", path, body=body)

    def put(self, path: str, body: dict) -> Any:
        return self.request("PUT", path, body=body)

    def patch(self, path: str, body: dict) -> Any:
        return self.request("PATCH", path, body=body)
