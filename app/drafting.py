"""Reply drafting + optional Claude classification.

Everything here degrades gracefully without ANTHROPIC_API_KEY:
 - ``ai_available()`` reports whether the optional AI path is usable.
 - ``classify_post`` returns None when AI is off (scanner keeps keyword score).
 - ``draft_reply`` returns a helpful template when AI is off.

The reply drafter is biased toward genuinely useful answers, not ad copy:
answer the poster's question first, mention CellSite Solutions at most once and
only when relevant, match forum tone. A human must approve before posting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import config

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
DRAFTER_MODEL = "claude-sonnet-5"

COMPANY_NAME = "CellSite Solutions"
COMPANY_URL = "https://www.cellsitesolutions.com"


@dataclass
class Classification:
    relevant: bool
    confidence: float
    one_line_summary: str


def ai_available() -> bool:
    return config.has_anthropic_key()


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=config.anthropic_key())


CLASSIFIER_SYSTEM = (
    "You classify forum/news posts for a B2B sales team at CellSite Solutions, "
    "a manufacturer and reseller of telecom shelters, fiber huts, equipment "
    "shelters, and modular/edge data centers. Decide whether a post is a genuine "
    "B2B infrastructure procurement or specification discussion relevant to such "
    "a seller (someone buying, spec'ing, sourcing, or discussing these products), "
    "as opposed to consumer chatter, unrelated IT, or generic news. "
    'Respond ONLY with JSON: {"relevant": bool, "confidence": 0-1 float, '
    '"one_line_summary": string}.'
)


def classify_post(title: str, body: str, forum_name: str) -> Classification | None:
    """Classify a post's relevance via Claude. Returns None if AI unavailable/fails."""
    if not ai_available():
        return None
    try:
        client = _client()
        prompt = (
            f"Forum: {forum_name}\nTitle: {title}\n\nBody:\n{body[:2000]}\n\n"
            "Classify this post."
        )
        resp = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=300,
            system=CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        data = _extract_json(text)
        if data is None:
            return None
        return Classification(
            relevant=bool(data.get("relevant", False)),
            confidence=float(data.get("confidence", 0.0)),
            one_line_summary=str(data.get("one_line_summary", "")).strip(),
        )
    except Exception:  # noqa: BLE001 - AI is best-effort; never break a scan
        return None


DRAFTER_SYSTEM = (
    f"You draft forum/community replies for a representative of {COMPANY_NAME} "
    f"({COMPANY_URL}), which supplies telecom shelters, fiber huts, equipment "
    "shelters, and modular/edge data centers. Rules: (1) Answer the poster's "
    "actual question first with genuine, specific, technically-credible help. "
    f"(2) Mention {COMPANY_NAME} at most once, with the link, and ONLY when it "
    "is genuinely relevant to what they asked — otherwise don't mention it at "
    "all. (3) No hype or ad copy; match the forum's tone. (4) Be concise. "
    "(5) Never fabricate specs, prices, or claims. The reply will be reviewed by "
    "a human before posting."
)


def draft_reply(
    title: str,
    body: str,
    forum_name: str,
    posting_notes: str = "",
    guidance: str = "",
) -> str:
    """Generate a reply draft. AI if available, else a helpful manual template."""
    if not ai_available():
        return _manual_template(title, forum_name)
    try:
        client = _client()
        notes = f"\nForum posting rules to respect: {posting_notes}" if posting_notes else ""
        extra = f"\nOperator guidance: {guidance}" if guidance else ""
        prompt = (
            f"Forum: {forum_name}{notes}{extra}\n\n"
            f"Post title: {title}\n\nPost body:\n{body[:2500]}\n\n"
            "Write a helpful reply."
        )
        resp = client.messages.create(
            model=DRAFTER_MODEL,
            max_tokens=700,
            system=DRAFTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text").strip()
    except Exception as exc:  # noqa: BLE001
        return _manual_template(title, forum_name) + f"\n\n[AI draft unavailable: {exc}]"


def _manual_template(title: str, forum_name: str) -> str:
    return (
        f"Re: {title}\n\n"
        "[Answer the poster's actual question here first — be specific and "
        "genuinely helpful.]\n\n"
        f"[Only if directly relevant, mention that {COMPANY_NAME} "
        f"({COMPANY_URL}) supplies this kind of equipment. Keep it to one line, "
        "no sales pitch. Delete this note before posting.]\n\n"
        f"(Drafting manually — set ANTHROPIC_API_KEY to enable AI drafts. "
        f"Respect {forum_name}'s self-promotion rules before posting.)"
    )


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
