"""Dedupe + seed idempotency tests using a temporary SQLite DB."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file and reset engine singletons."""
    from app import config, db as dbmod

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    dbmod._engine = None
    dbmod._SessionFactory = None
    dbmod.init_db()
    with dbmod.session() as s:
        dbmod.seed_forums(s)
        dbmod.seed_keywords(s)
    yield dbmod
    dbmod._engine = None
    dbmod._SessionFactory = None


def test_seed_is_idempotent(temp_db):
    from sqlalchemy import func, select
    from app.db import Forum

    with temp_db.session() as s:
        count1 = s.scalar(select(func.count(Forum.id)))
        temp_db.seed_forums(s)  # run again
        count2 = s.scalar(select(func.count(Forum.id)))
    assert count1 == count2 == 28


def test_scan_dedupes_on_external_id(temp_db, monkeypatch):
    """Two scans returning the same external_id create exactly one Post."""
    from sqlalchemy import func, select
    from app import scanner
    from app.adapters.base import RawPost
    from app.db import Forum, Post

    raw = RawPost(
        external_id="topic-1",
        title="Looking for a telecom shelter vendor",
        url="https://example.com/t/1",
        body="We need a quote for a telecom shelter, prefab.",
        posted_at=datetime.now(timezone.utc),
    )

    class StubAdapter:
        def __init__(self, forum, credentials=None):
            self.forum = forum

        def fetch_recent(self, since):
            return [raw, raw]  # same post twice in one scan

    monkeypatch.setattr(scanner, "build_adapter", lambda forum, credentials=None: StubAdapter(forum))

    with temp_db.session() as s:
        forum = s.scalar(select(Forum).where(Forum.slug == "telecom-hall"))
        scanner.scan_forum(s, forum)
        scanner.scan_forum(s, forum)  # scan again
        n = s.scalar(select(func.count(Post.id)).where(Post.forum_id == forum.id))
    assert n == 1


def test_scored_post_persists_band_and_keywords(temp_db, monkeypatch):
    from sqlalchemy import select
    from app import scanner
    from app.adapters.base import RawPost
    from app.db import Forum, Post

    raw = RawPost(
        external_id="topic-42",
        title="Telecom shelter RFP — need vendor quote",
        url="https://example.com/t/42",
        body="Seeking a prefab telecom shelter. Please send a quote. RFP open.",
        posted_at=datetime.now(timezone.utc),
    )

    class StubAdapter:
        def __init__(self, forum, credentials=None):
            self.forum = forum

        def fetch_recent(self, since):
            return [raw]

    monkeypatch.setattr(scanner, "build_adapter", lambda forum, credentials=None: StubAdapter(forum))

    with temp_db.session() as s:
        forum = s.scalar(select(Forum).where(Forum.slug == "telecom-hall"))
        scanner.scan_forum(s, forum)
        post = s.scalar(select(Post).where(Post.external_id == "topic-42"))
    assert post is not None
    assert post.score > 0
    assert post.score_band in ("HIGH", "MEDIUM", "LOW")
    assert any(kw["term"] == "telecom shelter" for kw in post.matched_keywords)


def test_competitor_mentions_are_tracked(temp_db, monkeypatch):
    from sqlalchemy import select
    from app import scanner
    from app.adapters.base import RawPost
    from app.db import Forum, Post

    raw = RawPost(
        external_id="topic-99",
        title="Comparing telecom shelter vendors",
        url="https://example.com/t/99",
        body="We got quotes from Fibrebond and Sabre for a prefab telecom shelter. Thoughts?",
        posted_at=datetime.now(timezone.utc),
    )

    class StubAdapter:
        def __init__(self, forum, credentials=None):
            self.forum = forum

        def fetch_recent(self, since):
            return [raw]

    monkeypatch.setattr(scanner, "build_adapter", lambda forum, credentials=None: StubAdapter(forum))

    with temp_db.session() as s:
        forum = s.scalar(select(Forum).where(Forum.slug == "telecom-hall"))
        scanner.scan_forum(s, forum)
        post = s.scalar(select(Post).where(Post.external_id == "topic-99"))
    assert post is not None
    assert set(post.matched_competitors) == {"Fibrebond", "Sabre"}


def test_topics_and_non_us_downweight(temp_db, monkeypatch):
    from sqlalchemy import select
    from app import scanner
    from app.adapters.base import RawPost
    from app.db import Forum, Post

    us_raw = RawPost(
        external_id="us-1", title="Fiber hut RFP in rural Texas (BEAD)",
        url="https://x/us", body="Seeking a fiber hut vendor for a BEAD build in Texas. Quote please.",
        posted_at=datetime.now(timezone.utc),
    )
    uk_raw = RawPost(
        external_id="uk-1", title="Fiber hut RFP in the United Kingdom",
        url="https://x/uk", body="Openreach project in the United Kingdom needs a fiber hut. Quote please.",
        posted_at=datetime.now(timezone.utc),
    )

    class StubAdapter:
        def __init__(self, forum, credentials=None):
            self.forum = forum

        def fetch_recent(self, since):
            return [us_raw, uk_raw]

    monkeypatch.setattr(scanner, "build_adapter", lambda forum, credentials=None: StubAdapter(forum))

    with temp_db.session() as s:
        forum = s.scalar(select(Forum).where(Forum.slug == "telecom-hall"))
        scanner.scan_forum(s, forum)
        us = s.scalar(select(Post).where(Post.external_id == "us-1"))
        uk = s.scalar(select(Post).where(Post.external_id == "uk-1"))

    assert "FIBER" in us.topics
    assert us.geo == "USA"
    assert uk.geo == "NON_USA"
    # USA-only is on by default -> the non-US post is heavily down-weighted.
    assert uk.score < us.score


def test_reanalyze_applies_ai_and_lifts_latent_opportunity(temp_db, monkeypatch):
    """A weak-keyword latent post is lifted once AI classifies it as an opportunity,
    and re-analyze preserves the operator's status."""
    from sqlalchemy import select
    from app import scanner, drafting
    from app.adapters.base import RawPost
    from app.db import Forum, Post
    from app.drafting import Classification

    raw = RawPost(
        external_id="latent-1",
        title="County broadband authority expanding rural fiber footprint",
        url="https://x/latent",
        body="Ohio county broadband authority expanding its fiber network under BEAD.",
        posted_at=datetime.now(timezone.utc),
    )

    class StubAdapter:
        def __init__(self, forum, credentials=None):
            self.forum = forum

        def fetch_recent(self, since):
            return [raw]

    monkeypatch.setattr(scanner, "build_adapter", lambda forum, credentials=None: StubAdapter(forum))

    with temp_db.session() as s:
        forum = s.scalar(select(Forum).where(Forum.slug == "telecom-hall"))
        scanner.scan_forum(s, forum)
        p = s.scalar(select(Post).where(Post.external_id == "latent-1"))
        p.status = "lead"
        s.commit()
        before_band = p.score_band

    # Simulate AI becoming available and calling it "related".
    monkeypatch.setattr(drafting, "ai_available", lambda: True)
    monkeypatch.setattr(
        drafting,
        "classify_post",
        lambda title, body, forum_name, context="": Classification(
            relevant=True, confidence=0.9, one_line_summary="Latent fiber-hut demand.",
            opportunity_type="related", is_usa=True,
        ),
    )
    scanner.reanalyze_all()

    with temp_db.session() as s:
        p = s.scalar(select(Post).where(Post.external_id == "latent-1"))
    assert before_band == "LOW"
    assert p.opportunity_type == "related"
    assert p.score_band in ("MEDIUM", "HIGH")
    assert p.ai_summary == "Latent fiber-hut demand."
    assert p.status == "lead"  # operator's mark preserved
