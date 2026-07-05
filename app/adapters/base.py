"""Adapter interface + shared polite HTTP client.

Every monitoring adapter implements ``fetch_recent(since) -> list[RawPost]``.
Posting-capable adapters also implement ``post_reply(...)``.

Politeness is enforced here so every adapter inherits it:
- honest User-Agent
- >= 5s between requests to the same host
- robots.txt respected for scrape-style fetching
- exponential backoff on 429 / 5xx
"""

from __future__ import annotations

import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import config


@dataclass
class RawPost:
    """A normalized post returned by an adapter, before persistence/scoring."""

    external_id: str
    title: str
    url: str
    author: str = ""
    body: str = ""
    posted_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PostResult:
    ok: bool
    url: str = ""
    error: str = ""


# --- Per-host rate limiting (process-wide) ---------------------------------

_host_locks: dict[str, threading.Lock] = {}
_host_last_request: dict[str, float] = {}
_registry_lock = threading.Lock()


def _host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def _throttle(url: str) -> None:
    host = _host_of(url)
    with _registry_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        last = _host_last_request.get(host, 0.0)
        wait = config.MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _host_last_request[host] = time.monotonic()


class PoliteClient:
    """Thin wrapper over httpx enforcing throttle + backoff + honest UA."""

    def __init__(self, timeout: float | None = None) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": config.USER_AGENT},
            timeout=timeout or config.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def can_fetch(self, url: str) -> bool:
        """Respect robots.txt. Fail open only when robots is unreachable."""
        host = _host_of(url)
        parsed = urlparse(url)
        if host not in self._robots_cache:
            robots_url = f"{parsed.scheme}://{host}/robots.txt"
            rp = urllib.robotparser.RobotFileParser()
            try:
                _throttle(robots_url)
                resp = self._client.get(robots_url)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                    self._robots_cache[host] = rp
                else:
                    # No robots or blocked fetch of robots -> allow (fail open).
                    self._robots_cache[host] = None
            except httpx.HTTPError:
                self._robots_cache[host] = None
        rp = self._robots_cache[host]
        if rp is None:
            return True
        return rp.can_fetch(config.USER_AGENT, url)

    def get(self, url: str, *, max_retries: int = 4, **kwargs: Any) -> httpx.Response:
        """GET with per-host throttle and exponential backoff on 429/5xx."""
        delay = 2.0
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            _throttle(url)
            try:
                resp = self._client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if (retry_after or "").isdigit() else delay
                time.sleep(sleep_for)
                delay *= 2
                continue
            return resp
        if last_exc:
            raise last_exc
        raise httpx.HTTPError(f"Exhausted retries fetching {url}")


class Adapter:
    """Base adapter. Subclasses set ``adapter_type`` and implement fetch_recent."""

    adapter_type: str = "base"

    def __init__(self, forum: Any) -> None:
        # ``forum`` is a db.Forum instance (duck-typed to keep this module import-light).
        self.forum = forum
        self.cfg: dict[str, Any] = forum.adapter_config

    def fetch_recent(self, since: datetime) -> list[RawPost]:  # pragma: no cover - interface
        raise NotImplementedError

    def post_reply(self, post: Any, body: str, credentials: dict[str, Any]) -> PostResult:
        """Default: adapter does not support programmatic posting."""
        return PostResult(ok=False, error=f"{self.adapter_type} adapter does not support posting.")
