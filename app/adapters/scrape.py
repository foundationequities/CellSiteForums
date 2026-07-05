"""Generic polite HTML forum scraper with per-forum CSS selectors.

adapter_config keys (all optional; sensible fallbacks applied):
  listing_urls : list[str]   pages listing threads (defaults to forum.url)
  item_selector: str         CSS selector for each thread row
  title_selector, link_selector, date_selector : str   (relative to item)
  date_format  : str         strptime format for the date text
  base_url     : str         to resolve relative links (defaults to forum.url)
  require_keyword_match: bool (handled by the scanner, kept for reference)

Politeness (robots.txt, >=5s/host, backoff, honest UA) comes from PoliteClient.
Posting is assisted-only for scrape forums (see PROJECT_BRIEF compliance rules):
``post_reply`` intentionally returns not-supported so the UI uses the
copy-to-clipboard workflow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import Adapter, PoliteClient, PostResult, RawPost

DEFAULT_ITEM_SELECTORS = [
    "li.thread",
    "tr.thread",
    "div.thread",
    "div.topic",
    "li.topic",
    "article",
    "div.structItem",  # XenForo
    "tr.topic",
]


class ScrapeBlockedByRobots(RuntimeError):
    pass


def parse_listing(
    html: str,
    *,
    base_url: str,
    item_selector: str | None = None,
    title_selector: str | None = None,
    link_selector: str | None = None,
    date_selector: str | None = None,
    date_format: str | None = None,
) -> list[RawPost]:
    """Parse a forum listing page into RawPosts. Pure function for fixture tests."""
    soup = BeautifulSoup(html, "html.parser")

    items = []
    if item_selector:
        items = soup.select(item_selector)
    else:
        for sel in DEFAULT_ITEM_SELECTORS:
            items = soup.select(sel)
            if items:
                break

    posts: list[RawPost] = []
    for item in items:
        # Link + title.
        link_el = item.select_one(link_selector) if link_selector else None
        if link_el is None:
            link_el = item.find("a", href=True)
        if link_el is None:
            continue
        href = link_el.get("href", "")
        url = urljoin(base_url + "/", href) if href else ""

        if title_selector:
            title_el = item.select_one(title_selector)
            title = title_el.get_text(strip=True) if title_el else ""
        else:
            title = link_el.get_text(strip=True)
        if not title:
            continue

        posted_at = None
        if date_selector:
            date_el = item.select_one(date_selector)
            if date_el is not None:
                posted_at = _parse_date(date_el, date_format)

        external_id = url or title
        posts.append(
            RawPost(
                external_id=external_id,
                title=title,
                url=url,
                posted_at=posted_at,
                body="",
            )
        )
    return posts


def _parse_date(el, date_format: str | None) -> datetime | None:
    # Prefer a machine-readable <time datetime=...> attribute.
    dt_attr = el.get("datetime") if hasattr(el, "get") else None
    if dt_attr:
        try:
            return datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
        except ValueError:
            pass
    text = el.get_text(strip=True)
    if date_format and text:
        try:
            return datetime.strptime(text, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class ScrapeAdapter(Adapter):
    adapter_type = "scrape"

    def _listing_urls(self) -> list[str]:
        urls = self.cfg.get("listing_urls")
        if isinstance(urls, str):
            return [urls]
        if isinstance(urls, list) and urls:
            return urls
        sites = self.cfg.get("sites")
        if isinstance(sites, list) and sites:
            return sites
        return [self.forum.url] if self.forum.url else []

    def fetch_recent(self, since: datetime) -> list[RawPost]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        base_url = (self.cfg.get("base_url") or self.forum.url or "").rstrip("/")
        listing_urls = self._listing_urls()
        if not listing_urls:
            return []

        seen: dict[str, RawPost] = {}
        with PoliteClient() as client:
            for listing_url in listing_urls:
                if not client.can_fetch(listing_url):
                    raise ScrapeBlockedByRobots(f"robots.txt disallows {listing_url}")
                try:
                    resp = client.get(listing_url)
                except httpx.HTTPError:
                    continue
                if resp.status_code != 200:
                    continue
                parsed = parse_listing(
                    resp.text,
                    base_url=base_url or listing_url,
                    item_selector=self.cfg.get("item_selector"),
                    title_selector=self.cfg.get("title_selector"),
                    link_selector=self.cfg.get("link_selector"),
                    date_selector=self.cfg.get("date_selector"),
                    date_format=self.cfg.get("date_format"),
                )
                for rp in parsed:
                    # If we have a date, honor the window; if not, keep (scanner scores it).
                    if rp.posted_at and rp.posted_at < since:
                        continue
                    seen[rp.external_id] = rp
        return list(seen.values())

    def post_reply(self, post, body: str, credentials: dict) -> PostResult:
        return PostResult(
            ok=False,
            error="Scrape-type forums use assisted posting (copy draft, paste manually).",
        )
