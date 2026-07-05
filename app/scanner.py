"""Scan orchestration: fetch → dedupe → score → optional AI → persist.

A scan never crashes the app: per-forum failures are caught and recorded in
``Forum.last_scan_status`` so the dashboard shows what happened.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from . import drafting, scoring
from .adapters import build_adapter
from .credentials import get_reddit_credentials
from .db import Forum, Post, session, utcnow, load_runtime_settings

logger = logging.getLogger("forumagent.scanner")


@dataclass
class ForumScanResult:
    forum_slug: str
    fetched: int = 0
    new_posts: int = 0
    skipped_existing: int = 0
    error: str = ""


@dataclass
class ScanSummary:
    results: list[ForumScanResult]

    @property
    def total_new(self) -> int:
        return sum(r.new_posts for r in self.results)

    @property
    def total_fetched(self) -> int:
        return sum(r.fetched for r in self.results)


def _load_keyword_specs(db) -> tuple[list[scoring.KeywordSpec], list[str]]:
    from .db import Keyword

    rows = db.scalars(select(Keyword).where(Keyword.enabled == True)).all()  # noqa: E712
    specs: list[scoring.KeywordSpec] = []
    boosters: list[str] = []
    for kw in rows:
        if kw.is_booster:
            boosters.append(kw.term)
        else:
            specs.append(scoring.KeywordSpec(term=kw.term, weight=kw.weight, category=kw.category))
    return specs, boosters


def scan_forum(db, forum: Forum, *, settings=None) -> ForumScanResult:
    """Scan one forum, persist new scored posts. Returns a per-forum result."""
    settings = settings or load_runtime_settings(db)
    result = ForumScanResult(forum_slug=forum.slug)
    since = utcnow() - timedelta(days=settings.lookback_days)

    specs, boosters = _load_keyword_specs(db)

    credentials = None
    if forum.adapter_type == "reddit":
        credentials = get_reddit_credentials(db)
        if credentials is None:
            result.error = "Reddit credentials not configured."
            forum.last_scanned_at = utcnow()
            forum.last_scan_status = "skipped: no Reddit credentials"
            db.commit()
            return result

    try:
        adapter = build_adapter(forum, credentials=credentials)
        raw_posts = adapter.fetch_recent(since)
    except Exception as exc:  # noqa: BLE001 - record, never crash the scan
        logger.warning("Scan failed for %s: %s", forum.slug, exc)
        result.error = str(exc)
        forum.last_scanned_at = utcnow()
        forum.last_scan_status = f"error: {str(exc)[:300]}"
        db.commit()
        return result

    result.fetched = len(raw_posts)
    require_match = bool(forum.adapter_config.get("require_keyword_match"))

    for rp in raw_posts:
        existing = db.scalar(
            select(Post).where(Post.forum_id == forum.id, Post.external_id == rp.external_id)
        )
        if existing is not None:
            result.skipped_existing += 1
            continue

        sr = scoring.score_text(
            rp.title,
            rp.body,
            specs,
            boosters,
            viability=forum.viability,
            posted_at=rp.posted_at,
            half_life_days=settings.recency_half_life_days,
            threshold_high=settings.threshold_high,
            threshold_medium=settings.threshold_medium,
        )

        # Noisy subs/forums: only keep posts that actually matched a keyword.
        if require_match and not sr.matched:
            continue

        post = Post(
            forum_id=forum.id,
            external_id=rp.external_id,
            url=rp.url,
            title=rp.title[:600],
            author=rp.author[:200],
            body_excerpt=rp.body[:1500],
            posted_at=rp.posted_at,
            score=sr.score,
            score_band=sr.band,
            matched_keywords_json=json.dumps(sr.matched_as_dicts),
            status="new",
        )

        # Optional AI classification for MEDIUM+ posts.
        if drafting.ai_available() and sr.band in ("HIGH", "MEDIUM"):
            classification = drafting.classify_post(rp.title, rp.body, forum.name)
            if classification is not None:
                post.ai_summary = classification.one_line_summary
                post.ai_relevant = classification.relevant

        db.add(post)
        result.new_posts += 1

    forum.last_scanned_at = utcnow()
    if result.error:
        forum.last_scan_status = f"error: {result.error[:300]}"
    else:
        forum.last_scan_status = f"ok: {result.new_posts} new / {result.fetched} fetched"
    db.commit()
    return result


def scan_all(only_enabled: bool = True) -> ScanSummary:
    """Scan every (enabled) forum. Used by the scheduler and the 'Scan Now' button."""
    with session() as db:
        settings = load_runtime_settings(db)
        stmt = select(Forum)
        if only_enabled:
            stmt = stmt.where(Forum.enabled == True)  # noqa: E712
        forums = db.scalars(stmt).all()
        results = [scan_forum(db, f, settings=settings) for f in forums]
    return ScanSummary(results=results)


def scan_one(forum_id: int) -> ForumScanResult:
    with session() as db:
        forum = db.get(Forum, forum_id)
        if forum is None:
            return ForumScanResult(forum_slug="?", error="Forum not found")
        return scan_forum(db, forum)
