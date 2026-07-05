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
