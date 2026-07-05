"""Fixture-driven test for the scrape adapter's listing parser."""

from __future__ import annotations

from pathlib import Path

from app.adapters.scrape import parse_listing

FIXTURE = Path(__file__).parent / "fixtures" / "forum_listing.html"


def test_parse_listing_extracts_threads():
    html = FIXTURE.read_text(encoding="utf-8")
    posts = parse_listing(
        html,
        base_url="https://www.contractortalk.com",
        item_selector="div.structItem",
        title_selector="div.structItem-title a",
        link_selector="div.structItem-title a",
        date_selector="time.u-dt",
    )
    assert len(posts) == 3
    titles = [p.title for p in posts]
    assert "Looking for a fiber hut vendor for a rural BEAD build" in titles

    first = posts[0]
    assert first.url == "https://www.contractortalk.com/threads/looking-for-a-fiber-hut-vendor.12345/"
    assert first.posted_at is not None
    assert first.posted_at.year == 2026 and first.posted_at.month == 6 and first.posted_at.day == 20
    # external_id defaults to the URL for dedupe stability.
    assert first.external_id == first.url


def test_parse_listing_fallback_selectors():
    """Without explicit selectors, default item selectors still find threads."""
    html = FIXTURE.read_text(encoding="utf-8")
    posts = parse_listing(html, base_url="https://www.contractortalk.com")
    assert len(posts) == 3
