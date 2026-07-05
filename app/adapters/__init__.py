"""Adapter registry and factory."""

from __future__ import annotations

from typing import Any

from .base import Adapter, PostResult, RawPost
from .discourse import DiscourseAdapter
from .manual import ManualAdapter
from .reddit import RedditAdapter
from .rss import RSSAdapter
from .scrape import ScrapeAdapter

ADAPTER_TYPES = ("rss", "discourse", "reddit", "scrape", "manual")


def build_adapter(forum: Any, credentials: dict[str, Any] | None = None) -> Adapter:
    """Instantiate the adapter for a forum. ``credentials`` only used by reddit."""
    atype = forum.adapter_type
    if atype == "rss":
        return RSSAdapter(forum)
    if atype == "discourse":
        return DiscourseAdapter(forum)
    if atype == "reddit":
        return RedditAdapter(forum, credentials=credentials)
    if atype == "scrape":
        return ScrapeAdapter(forum)
    if atype == "manual":
        return ManualAdapter(forum)
    raise ValueError(f"Unknown adapter_type: {atype!r}")


__all__ = [
    "Adapter",
    "RawPost",
    "PostResult",
    "build_adapter",
    "ADAPTER_TYPES",
    "RSSAdapter",
    "DiscourseAdapter",
    "RedditAdapter",
    "ScrapeAdapter",
    "ManualAdapter",
]
