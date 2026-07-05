"""Keyword relevance scoring.

Formula (see PROJECT_BRIEF.md):
    score = Σ(keyword weight × occurrences, capped per keyword)
          + title-match bonus (title occurrences counted ×2)
          + buying-signal booster (+3 if any booster co-occurs with a match)
    then × forum-viability multiplier (STRONG 1.3 / GOOD 1.1 / MODERATE 1.0)
    then × recency decay (exponential, half-life configurable, default 21 days)

Bands: HIGH ≥ threshold_high (12), MEDIUM ≥ threshold_medium (6), else LOW.

Matching is case-insensitive, word-boundary aware, with simple plural handling
(``shelter`` also matches ``shelters``; ``vault`` matches ``vaults``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

# Per-keyword occurrence cap so one spammy repetition can't dominate.
PER_KEYWORD_CAP = 3
BOOSTER_BONUS = 3.0
TITLE_MULTIPLIER = 2  # title occurrences count double

VIABILITY_MULTIPLIER = {"STRONG": 1.3, "GOOD": 1.1, "MODERATE": 1.0}


@dataclass
class KeywordSpec:
    term: str
    weight: float
    category: str = ""


@dataclass
class MatchedKeyword:
    term: str
    weight: float
    count: int
    in_title: bool


@dataclass
class ScoreResult:
    score: float
    band: str
    matched: list[MatchedKeyword] = field(default_factory=list)
    boosters_hit: list[str] = field(default_factory=list)

    @property
    def matched_as_dicts(self) -> list[dict]:
        return [
            {"term": m.term, "weight": m.weight, "count": m.count, "in_title": m.in_title}
            for m in self.matched
        ]


@lru_cache(maxsize=2048)
def _compile(term: str) -> re.Pattern[str]:
    """Compile a case-insensitive, word-boundary pattern with simple pluralization.

    Multi-word phrases allow flexible internal whitespace. A trailing optional
    ``(e)s`` handles simple plurals without matching unrelated longer words.
    """
    words = term.strip().split()
    escaped = r"\s+".join(re.escape(w) for w in words)
    # \b-style boundaries that treat digits/letters as "word" chars.
    pattern = rf"(?<![A-Za-z0-9]){escaped}(?:es|s)?(?![A-Za-z0-9])"
    return re.compile(pattern, re.IGNORECASE)


def count_occurrences(term: str, text: str) -> int:
    if not text:
        return 0
    return len(_compile(term).findall(text))


def _age_days(posted_at: datetime | None, now: datetime) -> float:
    if posted_at is None:
        return 0.0
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    delta = now - posted_at
    return max(0.0, delta.total_seconds() / 86400.0)


def recency_decay(posted_at: datetime | None, half_life_days: float, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    age = _age_days(posted_at, now)
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age / half_life_days)


def score_text(
    title: str,
    body: str,
    keywords: list[KeywordSpec],
    boosters: list[str],
    *,
    viability: str = "MODERATE",
    posted_at: datetime | None = None,
    half_life_days: float = 21.0,
    threshold_high: float = 12.0,
    threshold_medium: float = 6.0,
    now: datetime | None = None,
) -> ScoreResult:
    """Score a single post. Pure function — no I/O, unit-testable."""
    now = now or datetime.now(timezone.utc)
    title = title or ""
    body = body or ""

    matched: list[MatchedKeyword] = []
    keyword_score = 0.0

    for spec in keywords:
        occ_title = count_occurrences(spec.term, title)
        occ_body = count_occurrences(spec.term, body)
        total = occ_title + occ_body
        if total == 0:
            continue
        capped = min(total, PER_KEYWORD_CAP)
        contribution = spec.weight * capped
        # Title bonus: title occurrences count double (add their weight once more).
        title_bonus = spec.weight * min(occ_title, PER_KEYWORD_CAP) * (TITLE_MULTIPLIER - 1)
        keyword_score += contribution + title_bonus
        matched.append(
            MatchedKeyword(
                term=spec.term,
                weight=spec.weight,
                count=total,
                in_title=occ_title > 0,
            )
        )

    if not matched:
        return ScoreResult(score=0.0, band="LOW", matched=[], boosters_hit=[])

    # Buying-signal boosters: flat bonus if any co-occur with a keyword match.
    combined = f"{title}\n{body}"
    boosters_hit = [b for b in boosters if count_occurrences(b, combined) > 0]
    booster_bonus = BOOSTER_BONUS if boosters_hit else 0.0

    subtotal = keyword_score + booster_bonus
    subtotal *= VIABILITY_MULTIPLIER.get(viability, 1.0)
    subtotal *= recency_decay(posted_at, half_life_days, now)

    score = round(subtotal, 2)
    if score >= threshold_high:
        band = "HIGH"
    elif score >= threshold_medium:
        band = "MEDIUM"
    else:
        band = "LOW"

    return ScoreResult(score=score, band=band, matched=matched, boosters_hit=boosters_hit)


def band_for_score(score: float, threshold_high: float, threshold_medium: float) -> str:
    if score >= threshold_high:
        return "HIGH"
    if score >= threshold_medium:
        return "MEDIUM"
    return "LOW"
