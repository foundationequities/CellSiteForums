"""Scan orchestration: fetch → dedupe → score → optional AI → persist.

A scan never crashes the app: per-forum failures are caught and recorded in
``Forum.last_scan_status`` so the dashboard shows what happened.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from . import config, drafting, scoring
from .adapters import build_adapter
from .credentials import get_reddit_credentials
from .db import Forum, Post, get_setting, session, set_setting, utcnow, load_runtime_settings

logger = logging.getLogger("forumagent.scanner")

# How hard to down-weight a post identified as non-U.S. when USA-only is on.
NON_US_PENALTY = 0.15

# --- Background scan state (so the UI never blocks on a long scan) -----------

_scan_lock = threading.Lock()
_scan_state: dict = {
    "running": False,
    "label": "scan",  # "scan" | "reanalyze"
    "started_at": None,
    "finished_at": None,
    "new_posts": 0,
    "fetched": 0,
    "error": "",
}


def scan_status() -> dict:
    with _scan_lock:
        return dict(_scan_state)


def _background_scan(since: datetime | None) -> None:
    try:
        summary = scan_all(since=since)
        with _scan_lock:
            _scan_state.update(
                running=False,
                finished_at=utcnow(),
                new_posts=summary.total_new,
                fetched=summary.total_fetched,
                error="",
            )
        logger.info("Background scan finished: %s new posts.", summary.total_new)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Background scan failed: %s", exc)
        with _scan_lock:
            _scan_state.update(running=False, finished_at=utcnow(), error=str(exc)[:300])


def start_scan(since: datetime | None = None) -> bool:
    """Kick off a scan in a daemon thread. Returns False if one is already running."""
    with _scan_lock:
        if _scan_state["running"]:
            return False
        _scan_state.update(
            running=True, label="scan", started_at=utcnow(), finished_at=None,
            new_posts=0, fetched=0, error="",
        )
    threading.Thread(target=_background_scan, args=(since,), daemon=True).start()
    return True


def _background_reanalyze() -> None:
    try:
        updated = reanalyze_all()
        with _scan_lock:
            _scan_state.update(
                running=False, finished_at=utcnow(), new_posts=updated, fetched=updated, error=""
            )
        logger.info("Re-analyze finished: %s posts updated.", updated)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Re-analyze failed: %s", exc)
        with _scan_lock:
            _scan_state.update(running=False, finished_at=utcnow(), error=str(exc)[:300])


def start_reanalyze() -> bool:
    """Re-score + re-classify already-stored posts (e.g. after adding an API key
    or editing the intuition context). Runs in the background; keeps statuses."""
    with _scan_lock:
        if _scan_state["running"]:
            return False
        _scan_state.update(
            running=True, label="reanalyze", started_at=utcnow(), finished_at=None,
            new_posts=0, fetched=0, error="",
        )
    threading.Thread(target=_background_reanalyze, daemon=True).start()
    return True


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


def _load_keyword_specs(db) -> tuple[list[scoring.KeywordSpec], list[str], list[str]]:
    from .db import Keyword

    rows = db.scalars(select(Keyword).where(Keyword.enabled == True)).all()  # noqa: E712
    specs: list[scoring.KeywordSpec] = []
    boosters: list[str] = []
    competitors: list[str] = []
    for kw in rows:
        if kw.is_competitor:
            competitors.append(kw.term)
        elif kw.is_booster:
            boosters.append(kw.term)
        else:
            specs.append(scoring.KeywordSpec(term=kw.term, weight=kw.weight, category=kw.category))
    return specs, boosters, competitors


@dataclass
class Analysis:
    sr: "scoring.ScoreResult"
    matched_competitors: list[str]
    classification: object | None
    geo: str
    score: float
    band: str
    opportunity_type: str


def analyze(
    title: str,
    body: str,
    forum_name: str,
    viability: str,
    posted_at,
    *,
    specs,
    boosters,
    competitors,
    us_signals,
    non_us_signals,
    ai_context,
    usa_only,
    settings,
    ai_scope="matched",
) -> Analysis:
    """Score + competitor + geography + (optional) AI opportunity analysis.

    Shared by the scanner (new posts) and the re-analyze action (stored posts).

    ``ai_scope`` controls how broadly the AI classifier runs when a key is set:
      "matched"     - any post that hit >=1 keyword or competitor (default);
      "medium_plus" - only keyword-MEDIUM+ posts (cheapest);
      "all"         - every fetched post (most thorough / most API calls).
    Running on "matched" (not just MEDIUM+) is what lets the AI catch latent
    opportunities — e.g. a fiber-footprint expansion that only weakly matches
    keywords — and its verdict then lifts the post's rank so it surfaces.
    """
    sr = scoring.score_text(
        title,
        body,
        specs,
        boosters,
        viability=viability,
        posted_at=posted_at,
        half_life_days=settings.recency_half_life_days,
        threshold_high=settings.threshold_high,
        threshold_medium=settings.threshold_medium,
    )
    combined = f"{title}\n{body}"
    matched_competitors = [c for c in competitors if scoring.count_occurrences(c, combined) > 0]

    run_ai = drafting.ai_available() and (
        ai_scope == "all"
        or (ai_scope == "matched" and (bool(sr.matched) or bool(matched_competitors)))
        or (ai_scope == "medium_plus" and sr.band in ("HIGH", "MEDIUM"))
    )
    classification = (
        drafting.classify_post(title, body, forum_name, context=ai_context) if run_ai else None
    )

    geo = scoring.usa_geography(combined, us_signals, non_us_signals)
    if classification is not None and classification.is_usa is not None:
        geo = scoring.GEO_USA if classification.is_usa else scoring.GEO_NON_USA

    score = sr.score
    # An AI opportunity verdict lifts the rank so latent leads surface even with
    # few keyword hits (a direct hit floors to HIGH, a related hit to MEDIUM).
    if classification is not None:
        if classification.opportunity_type == "direct":
            score = max(score, settings.threshold_high)
        elif classification.opportunity_type == "related":
            score = max(score, settings.threshold_medium)
    # USA-only focus: down-weight anything clearly non-U.S. AFTER any AI lift.
    if usa_only and geo == scoring.GEO_NON_USA:
        score = round(score * NON_US_PENALTY, 2)
    band = scoring.band_for_score(score, settings.threshold_high, settings.threshold_medium)

    if classification is not None and classification.opportunity_type:
        opportunity_type = classification.opportunity_type
    else:
        opportunity_type = {"HIGH": "direct", "MEDIUM": "related"}.get(band, "none")

    return Analysis(
        sr=sr,
        matched_competitors=matched_competitors,
        classification=classification,
        geo=geo,
        score=score,
        band=band,
        opportunity_type=opportunity_type,
    )


def scan_forum(db, forum: Forum, *, settings=None, since: datetime | None = None) -> ForumScanResult:
    """Scan one forum, persist new scored posts. Returns a per-forum result.

    ``since`` overrides the lookback start (used for incremental scans). When
    omitted, the full ``lookback_days`` window is used.
    """
    settings = settings or load_runtime_settings(db)
    result = ForumScanResult(forum_slug=forum.slug)
    if since is None:
        since = utcnow() - timedelta(days=settings.lookback_days)

    specs, boosters, competitors = _load_keyword_specs(db)
    us_signals, non_us_signals = config.load_geo_signals()
    ai_context = get_setting(db, "ai_context", config.DEFAULT_AI_CONTEXT)
    usa_only = get_setting(db, "usa_only", "1") == "1"
    ai_scope = get_setting(db, "ai_scope", "matched")

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

        a = analyze(
            rp.title, rp.body, forum.name, forum.viability, rp.posted_at,
            specs=specs, boosters=boosters, competitors=competitors,
            us_signals=us_signals, non_us_signals=non_us_signals,
            ai_context=ai_context, usa_only=usa_only, settings=settings, ai_scope=ai_scope,
        )

        # Noisy subs/forums: keep only posts that matched a keyword OR name a competitor.
        if require_match and not a.sr.matched and not a.matched_competitors:
            continue

        post = Post(
            forum_id=forum.id,
            external_id=rp.external_id,
            url=rp.url,
            title=rp.title[:600],
            author=rp.author[:200],
            body_excerpt=rp.body[:1500],
            posted_at=rp.posted_at,
            score=a.score,
            score_band=a.band,
            matched_keywords_json=json.dumps(a.sr.matched_as_dicts),
            matched_competitors_json=json.dumps(a.matched_competitors),
            topics_json=json.dumps(a.sr.topics),
            geo=a.geo,
            opportunity_type=a.opportunity_type,
            status="new",
        )
        if a.classification is not None:
            post.ai_summary = a.classification.one_line_summary
            post.ai_relevant = a.classification.relevant

        db.add(post)
        result.new_posts += 1

    forum.last_scanned_at = utcnow()
    if result.error:
        forum.last_scan_status = f"error: {result.error[:300]}"
    else:
        forum.last_scan_status = f"ok: {result.new_posts} new / {result.fetched} fetched"
    db.commit()
    return result


def scan_all(only_enabled: bool = True, since: datetime | None = None) -> ScanSummary:
    """Scan every (enabled) forum. Used by the scheduler and the 'Scan Now' button.

    ``since`` overrides the per-forum lookback start (incremental scans). The
    completion time is recorded as the ``last_scan_at`` setting so future scans
    can default to "since last scan".
    """
    with session() as db:
        settings = load_runtime_settings(db)
        stmt = select(Forum)
        if only_enabled:
            stmt = stmt.where(Forum.enabled == True)  # noqa: E712
        forums = db.scalars(stmt).all()
        results = [scan_forum(db, f, settings=settings, since=since) for f in forums]
        set_setting(db, "last_scan_at", utcnow().isoformat())
    return ScanSummary(results=results)


def scan_one(forum_id: int, since: datetime | None = None) -> ForumScanResult:
    with session() as db:
        forum = db.get(Forum, forum_id)
        if forum is None:
            return ForumScanResult(forum_slug="?", error="Forum not found")
        return scan_forum(db, forum, since=since)


def reanalyze_all() -> int:
    """Re-run scoring + geography + (optional) AI opportunity analysis over every
    stored post, in place. Does not re-fetch forums and preserves post status,
    leads, and replies. Returns the number of posts updated.
    """
    with session() as db:
        settings = load_runtime_settings(db)
        specs, boosters, competitors = _load_keyword_specs(db)
        us_signals, non_us_signals = config.load_geo_signals()
        ai_context = get_setting(db, "ai_context", config.DEFAULT_AI_CONTEXT)
        usa_only = get_setting(db, "usa_only", "1") == "1"
        ai_scope = get_setting(db, "ai_scope", "matched")

        posts = db.scalars(select(Post)).all()
        for p in posts:
            forum = db.get(Forum, p.forum_id)
            a = analyze(
                p.title, p.body_excerpt, forum.name if forum else "",
                forum.viability if forum else "MODERATE", p.posted_at,
                specs=specs, boosters=boosters, competitors=competitors,
                us_signals=us_signals, non_us_signals=non_us_signals,
                ai_context=ai_context, usa_only=usa_only, settings=settings, ai_scope=ai_scope,
            )
            p.score = a.score
            p.score_band = a.band
            p.matched_keywords_json = json.dumps(a.sr.matched_as_dicts)
            p.matched_competitors_json = json.dumps(a.matched_competitors)
            p.topics_json = json.dumps(a.sr.topics)
            p.geo = a.geo
            p.opportunity_type = a.opportunity_type
            if a.classification is not None:
                p.ai_summary = a.classification.one_line_summary
                p.ai_relevant = a.classification.relevant
        db.commit()
        return len(posts)
