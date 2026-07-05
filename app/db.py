"""SQLAlchemy 2.x models, session factory, and idempotent config seeding."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from . import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Forum(Base):
    __tablename__ = "forums"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(20))  # TOWER/FIBER/DATA/TRADES
    viability: Mapped[str] = mapped_column(String(20))  # STRONG/GOOD/MODERATE
    adapter_type: Mapped[str] = mapped_column(String(30))
    adapter_config_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    can_post: Mapped[bool] = mapped_column(Boolean, default=False)
    posting_notes: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scan_status: Mapped[str] = mapped_column(String(400), default="")

    posts: Mapped[list["Post"]] = relationship(back_populates="forum")

    @property
    def adapter_config(self) -> dict[str, Any]:
        try:
            return json.loads(self.adapter_config_json or "{}")
        except json.JSONDecodeError:
            return {}


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("forum_id", "external_id", name="uq_forum_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forum_id: Mapped[int] = mapped_column(ForeignKey("forums.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(400), index=True)
    url: Mapped[str] = mapped_column(String(1000), default="")
    title: Mapped[str] = mapped_column(String(600), default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    score_band: Mapped[str] = mapped_column(String(10), default="LOW")  # HIGH/MEDIUM/LOW
    matched_keywords_json: Mapped[str] = mapped_column(Text, default="[]")
    matched_competitors_json: Mapped[str] = mapped_column(Text, default="[]")
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new/reviewed/lead/ignored

    forum: Mapped["Forum"] = relationship(back_populates="posts")
    replies: Mapped[list["Reply"]] = relationship(back_populates="post")

    @property
    def matched_keywords(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.matched_keywords_json or "[]")
        except json.JSONDecodeError:
            return []

    @property
    def matched_competitors(self) -> list[str]:
        try:
            return json.loads(self.matched_competitors_json or "[]")
        except json.JSONDecodeError:
            return []


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forum_id: Mapped[int | None] = mapped_column(ForeignKey("forums.id"), nullable=True, index=True)
    # kind: reddit_oauth / discourse_api_key / username_password
    kind: Mapped[str] = mapped_column(String(40))
    encrypted_blob: Mapped[bytes] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verify_status: Mapped[str] = mapped_column(String(200), default="unverified")

    forum: Mapped["Forum | None"] = relationship()


class Reply(Base):
    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    draft_body: Mapped[str] = mapped_column(Text, default="")
    final_body: Mapped[str] = mapped_column(Text, default="")
    # draft / approved / posted / failed / manual_copied
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    posted_url: Mapped[str] = mapped_column(String(1000), default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    post: Mapped["Post"] = relationship(back_populates="replies")


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    category: Mapped[str] = mapped_column(String(20), default="")
    is_booster: Mapped[bool] = mapped_column(Boolean, default=False)
    is_competitor: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# --- Engine / session -------------------------------------------------------

_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine():
    global _engine
    if _engine is None:
        config.ensure_data_dir()
        _engine = create_engine(
            f"sqlite:///{config.DB_PATH}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


def session() -> Session:
    return get_session_factory()()


def _run_lightweight_migrations() -> None:
    """Add columns introduced after a DB was first created (SQLite, no Alembic).

    Keeps existing local databases working without a manual reset.
    """
    engine = get_engine()
    with engine.begin() as conn:
        post_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(posts)").fetchall()}
        if "matched_competitors_json" not in post_cols:
            conn.exec_driver_sql(
                "ALTER TABLE posts ADD COLUMN matched_competitors_json TEXT DEFAULT '[]'"
            )
        kw_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(keywords)").fetchall()}
        if "is_competitor" not in kw_cols:
            conn.exec_driver_sql(
                "ALTER TABLE keywords ADD COLUMN is_competitor BOOLEAN DEFAULT 0"
            )


def init_db() -> None:
    Base.metadata.create_all(get_engine())
    _run_lightweight_migrations()


# --- Seeding (idempotent) ---------------------------------------------------


def seed_forums(db: Session) -> tuple[int, int]:
    """Insert/update forums from forums.json. Returns (added, updated).

    Idempotent: re-running never duplicates. Existing operator changes to
    ``enabled`` and ``notes`` are preserved; descriptive fields are refreshed
    from the seed file.
    """
    forums = config.load_forums()
    added = updated = 0
    for f in forums:
        existing = db.scalar(select(Forum).where(Forum.slug == f["slug"]))
        cfg_json = json.dumps(f.get("adapter_config", {}))
        if existing is None:
            db.add(
                Forum(
                    slug=f["slug"],
                    name=f["name"],
                    url=f.get("url", ""),
                    category=f["category"],
                    viability=f["viability"],
                    adapter_type=f["adapter_type"],
                    adapter_config_json=cfg_json,
                    enabled=f.get("enabled", True),
                    can_post=f.get("can_post", False),
                    posting_notes=f.get("posting_notes", ""),
                    notes=f.get("notes", ""),
                )
            )
            added += 1
        else:
            # Refresh descriptive fields but preserve operator-toggled enabled state.
            existing.name = f["name"]
            existing.url = f.get("url", "")
            existing.category = f["category"]
            existing.viability = f["viability"]
            existing.adapter_type = f["adapter_type"]
            existing.adapter_config_json = cfg_json
            existing.can_post = f.get("can_post", False)
            existing.posting_notes = f.get("posting_notes", "")
            existing.notes = f.get("notes", "")
            updated += 1
    db.commit()
    return added, updated


def seed_keywords(db: Session) -> int:
    """Insert keywords + boosters from keywords.json if the table is empty.

    Idempotent and non-destructive: once seeded, operator edits in Settings win,
    so we only add terms that don't already exist.
    """
    data = config.load_keywords()
    added = 0
    for kw in data.get("keywords", []):
        term = kw["term"]
        if db.scalar(select(Keyword).where(Keyword.term == term)) is None:
            db.add(
                Keyword(
                    term=term,
                    weight=float(kw.get("weight", 1.0)),
                    category=kw.get("category", ""),
                    is_booster=False,
                )
            )
            added += 1
    boosters = data.get("buying_signal_boosters", {}).get("terms", [])
    for term in boosters:
        if db.scalar(select(Keyword).where(Keyword.term == term)) is None:
            db.add(Keyword(term=term, weight=0.0, category="", is_booster=True))
            added += 1
    for term in config.load_competitors():
        if db.scalar(select(Keyword).where(Keyword.term == term)) is None:
            db.add(Keyword(term=term, weight=0.0, category="COMPETITOR", is_competitor=True))
            added += 1
    db.commit()
    return added


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def load_runtime_settings(db: Session) -> config.RuntimeSettings:
    s = config.RuntimeSettings()
    s.lookback_days = int(get_setting(db, "lookback_days", str(s.lookback_days)))
    s.scan_interval_hours = int(get_setting(db, "scan_interval_hours", str(s.scan_interval_hours)))
    s.recency_half_life_days = int(
        get_setting(db, "recency_half_life_days", str(s.recency_half_life_days))
    )
    s.threshold_high = float(get_setting(db, "threshold_high", str(s.threshold_high)))
    s.threshold_medium = float(get_setting(db, "threshold_medium", str(s.threshold_medium)))
    return s


def seed_all() -> None:
    init_db()
    with session() as db:
        seed_forums(db)
        seed_keywords(db)
