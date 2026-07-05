"""Discourse adapter (telecomhall.net). Reads via public JSON, posts via API.

Reading needs no auth:
  - GET /latest.json                      (recent topics)
  - GET /search.json?q=<kw>+after:<date>  (keyword-scoped recent)

Posting needs a per-user API key (Api-Key/Api-Username headers) OR a
username/password session; we use API-key headers for POST /posts.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .. import config
from .base import Adapter, PoliteClient, PostResult, RawPost


class DiscourseAdapter(Adapter):
    adapter_type = "discourse"

    @property
    def base_url(self) -> str:
        return (self.cfg.get("base_url") or self.forum.url or "").rstrip("/")

    def fetch_recent(self, since: datetime) -> list[RawPost]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        base = self.base_url
        if not base:
            return []

        seen: dict[str, RawPost] = {}
        with PoliteClient() as client:
            self._collect_latest(client, base, since, seen)
            # Also search top keywords to catch older-but-relevant threads in window.
            for term in self.cfg.get("search_terms", []):
                self._collect_search(client, base, term, since, seen)
        return list(seen.values())

    def _collect_latest(self, client, base, since, seen) -> None:
        try:
            resp = client.get(f"{base}/latest.json")
        except httpx.HTTPError:
            return
        if resp.status_code != 200:
            return
        data = resp.json()
        topic_list = data.get("topic_list", {}).get("topics", [])
        for t in topic_list:
            posted_at = _parse_dt(t.get("created_at"))
            if posted_at and posted_at < since:
                continue
            tid = str(t.get("id"))
            slug = t.get("slug", "")
            seen[tid] = RawPost(
                external_id=f"topic-{tid}",
                title=t.get("title", ""),
                url=f"{base}/t/{slug}/{tid}",
                author="",
                body=t.get("excerpt", "") or "",
                posted_at=posted_at,
                extra={"topic_id": t.get("id")},
            )

    def _collect_search(self, client, base, term, since, seen) -> None:
        after = since.strftime("%Y-%m-%d")
        q = quote(f"{term} after:{after}")
        try:
            resp = client.get(f"{base}/search.json?q={q}")
        except httpx.HTTPError:
            return
        if resp.status_code != 200:
            return
        data = resp.json()
        for t in data.get("topics", []):
            posted_at = _parse_dt(t.get("created_at"))
            if posted_at and posted_at < since:
                continue
            tid = str(t.get("id"))
            if f"topic-{tid}" in seen:
                continue
            slug = t.get("slug", "")
            seen[tid] = RawPost(
                external_id=f"topic-{tid}",
                title=t.get("title", ""),
                url=f"{base}/t/{slug}/{tid}",
                body="",
                posted_at=posted_at,
                extra={"topic_id": t.get("id")},
            )

    def post_reply(self, post, body: str, credentials: dict) -> PostResult:
        """POST a reply to the topic via the Discourse API.

        credentials: {"api_key": ..., "api_username": ...}
        The Post model stores the topic id in matched_keywords extra? We derive
        the topic id from the post URL or external_id.
        """
        api_key = credentials.get("api_key")
        api_username = credentials.get("api_username")
        if not api_key or not api_username:
            return PostResult(ok=False, error="Missing Discourse api_key/api_username.")

        topic_id = _topic_id_from_post(post)
        if not topic_id:
            return PostResult(ok=False, error="Could not determine Discourse topic id for reply.")

        base = self.base_url
        headers = {
            "Api-Key": api_key,
            "Api-Username": api_username,
            "User-Agent": config.USER_AGENT,
        }
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.post(
                    f"{base}/posts.json",
                    data={"topic_id": topic_id, "raw": body},
                )
        except httpx.HTTPError as exc:
            return PostResult(ok=False, error=f"Network error: {exc}")

        if resp.status_code in (200, 201):
            data = resp.json()
            post_number = data.get("post_number", "")
            url = f"{base}/t/{topic_id}/{post_number}" if post_number else post.url
            return PostResult(ok=True, url=url)
        return PostResult(ok=False, error=f"HTTP {resp.status_code}: {resp.text[:300]}")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _topic_id_from_post(post) -> str | None:
    ext = getattr(post, "external_id", "") or ""
    if ext.startswith("topic-"):
        return ext.split("-", 1)[1]
    url = getattr(post, "url", "") or ""
    # .../t/<slug>/<id> or .../t/<id>
    parts = [p for p in url.split("/") if p]
    for p in reversed(parts):
        if p.isdigit():
            return p
    return None
