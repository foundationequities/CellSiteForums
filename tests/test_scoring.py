"""Unit tests for the keyword scoring math."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import scoring
from app.scoring import KeywordSpec, score_text

KEYWORDS = [
    KeywordSpec("telecom shelter", 5, "TOWER"),
    KeywordSpec("fiber hut", 5, "FIBER"),
    KeywordSpec("cell site", 2, "TOWER"),
    KeywordSpec("modular data center", 4, "DATA"),
]
BOOSTERS = ["RFP", "quote", "vendor", "spec"]

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def _fresh(days_old: int = 0) -> datetime:
    return NOW - timedelta(days=days_old)


def test_no_match_scores_zero():
    r = score_text("Hello world", "nothing relevant here", KEYWORDS, BOOSTERS, now=NOW)
    assert r.score == 0.0
    assert r.band == "LOW"
    assert r.matched == []


def test_basic_weight_and_band():
    # "telecom shelter" weight 5, MODERATE viability, fresh -> 5.0 -> LOW (<6)
    r = score_text(
        "",
        "We need a telecom shelter for the site.",
        KEYWORDS,
        BOOSTERS,
        viability="MODERATE",
        posted_at=_fresh(0),
        now=NOW,
    )
    assert r.score == 5.0
    assert r.band == "LOW"
    assert any(m.term == "telecom shelter" for m in r.matched)


def test_title_match_doubles():
    body = score_text("", "a telecom shelter", KEYWORDS, BOOSTERS, posted_at=_fresh(0), now=NOW).score
    title = score_text("telecom shelter", "", KEYWORDS, BOOSTERS, posted_at=_fresh(0), now=NOW).score
    assert title == body * 2  # title occurrences count double


def test_plural_handling():
    r = score_text("", "several telecom shelters on order", KEYWORDS, BOOSTERS, posted_at=_fresh(0), now=NOW)
    assert any(m.term == "telecom shelter" for m in r.matched)


def test_per_keyword_cap():
    text = "fiber hut " * 10  # 10 occurrences, capped at 3
    r = score_text("", text, KEYWORDS, BOOSTERS, posted_at=_fresh(0), now=NOW)
    # weight 5 * cap 3 = 15
    assert r.score == 15.0


def test_booster_bonus_applies_with_match():
    with_boost = score_text(
        "telecom shelter RFP", "vendor quote please", KEYWORDS, BOOSTERS, posted_at=_fresh(0), now=NOW
    )
    assert with_boost.boosters_hit  # RFP/vendor/quote present
    # title match: 5*1 (body 0) title double => 10, +3 booster = 13 -> HIGH
    assert with_boost.band == "HIGH"


def test_booster_ignored_without_keyword_match():
    r = score_text("RFP quote vendor", "spec sourcing", KEYWORDS, BOOSTERS, posted_at=_fresh(0), now=NOW)
    assert r.score == 0.0  # boosters alone never score


def test_viability_multiplier():
    strong = score_text("telecom shelter", "", KEYWORDS, BOOSTERS, viability="STRONG", posted_at=_fresh(0), now=NOW)
    moderate = score_text("telecom shelter", "", KEYWORDS, BOOSTERS, viability="MODERATE", posted_at=_fresh(0), now=NOW)
    assert strong.score > moderate.score
    assert round(strong.score / moderate.score, 2) == 1.3


def test_recency_decay_halves_at_half_life():
    fresh = score_text("telecom shelter", "", KEYWORDS, BOOSTERS, posted_at=_fresh(0), half_life_days=21, now=NOW)
    aged = score_text("telecom shelter", "", KEYWORDS, BOOSTERS, posted_at=_fresh(21), half_life_days=21, now=NOW)
    assert round(aged.score / fresh.score, 2) == 0.5


def test_word_boundary_no_partial_match():
    # "cell site" should not match inside "cellsite" (no space).
    r = score_text("", "cellsitesolutions dot com", KEYWORDS, BOOSTERS, posted_at=_fresh(0), now=NOW)
    assert not any(m.term == "cell site" for m in r.matched)


def test_bands_configurable():
    r = score_text(
        "telecom shelter", "", KEYWORDS, BOOSTERS, viability="MODERATE",
        posted_at=_fresh(0), threshold_high=8, threshold_medium=4, now=NOW,
    )
    # title match => 10 -> HIGH with threshold_high=8
    assert r.band == "HIGH"


def test_count_occurrences_helper():
    assert scoring.count_occurrences("fiber hut", "Fiber Hut and another fiber huts") == 2
