"""RSS/Atom adapter for trade publications. Monitor-only.

Feed URLs are not hardcoded blindly. The adapter, in order:
 1. uses explicit ``feed_urls`` from adapter_config if present,
 2. tries a set of conventional feed paths for the forum's base URL,
 3. auto-discovers via ``<link rel="alternate" type="application/rss+xml">``
    on the site homepage.
This keeps it working even though feed URLs could not be verified at build
time (restricted egress). If nothing resolves, it records the reason and
returns an empty list rather than crashing the scan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import mktime
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

from .base import Adapter, PoliteClient, RawPost

CONVENTIONAL_PATHS = [
    "/rss",
    "/rss/",
    "/feed",
    "/feed/",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/rss/all",
]


class RSSFeedError(RuntimeError):
    pass


class RSSAdapter(Adapter):
    adapter_type = "rss"

    def _candidate_urls(self, client: PoliteClient) -> list[str]:
        explicit = self.cfg.get("feed_urls") or self.cfg.get("feed_url")
        if isinstance(explicit, str):
            return [explicit]
        if isinstance(explicit, list) and explicit:
            return list(explicit)

        base = (self.forum.url or "").rstrip("/")
        candidates: list[str] = [base + path for path in CONVENTIONAL_PATHS] if base else []

        discovered = self._discover(client, base)
        return discovered + candidates

    def _discover(self, client: PoliteClient, base: str) -> list[str]:
        """Parse the homepage for declared feed links."""
        if not base:
            return []
        try:
            resp = client.get(base)
        except httpx.HTTPError:
            return []
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        found: list[str] = []
        for link in soup.find_all("link", rel="alternate"):
            ltype = (link.get("type") or "").lower()
            if "rss" in ltype or "atom" in ltype or "xml" in ltype:
                href = link.get("href")
                if href:
                    found.append(urljoin(base + "/", href))
        return found

    def fetch_recent(self, since: datetime) -> list[RawPost]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        posts: list[RawPost] = []
        with PoliteClient() as client:
            feed = None
            used_url = ""
            for url in self._candidate_urls(client):
                try:
                    resp = client.get(url)
                except httpx.HTTPError:
                    continue
                if resp.status_code != 200 or not resp.content:
                    continue
                parsed = feedparser.parse(resp.content)
                if parsed.entries:
                    feed = parsed
                    used_url = url
                    break
            if feed is None:
                raise RSSFeedError(
                    f"No resolvable RSS feed for {self.forum.slug} "
                    f"({self.forum.url}); verify feed URL and set adapter_config.feed_urls."
                )

            for entry in feed.entries:
                posted_at = _entry_datetime(entry)
                if posted_at and posted_at < since:
                    continue
                link = entry.get("link", "")
                external_id = entry.get("id") or link or entry.get("title", "")
                body = _entry_body(entry)
                posts.append(
                    RawPost(
                        external_id=str(external_id),
                        title=entry.get("title", "(untitled)"),
                        url=link,
                        author=entry.get("author", ""),
                        body=body,
                        posted_at=posted_at,
                        extra={"feed_url": used_url},
                    )
                )
        return posts


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime.fromtimestamp(mktime(val), tz=timezone.utc)
    return None


def _entry_body(entry) -> str:
    if entry.get("summary"):
        text = entry["summary"]
    elif entry.get("content"):
        text = entry["content"][0].get("value", "")
    else:
        text = ""
    if text and "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return text[:1500]
