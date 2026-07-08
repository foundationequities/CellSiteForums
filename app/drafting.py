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
    opportunity_type: str = ""  # "direct" | "related" | "none"
    is_usa: bool | None = None


def ai_available() -> bool:
    return config.has_anthropic_key()


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=config.anthropic_key())


CLASSIFIER_SYSTEM = (
    "You are a sales-intelligence analyst for CellSite Solutions. Read the "
    "operator's business context, then judge whether a forum/news post is a real "
    "opportunity — using intuition, not just keyword matching. Infer latent "
    "demand: if an organization is expanding fiber/telecom/data-center or E911 "
    "infrastructure, it will likely need a physical structure (fiber hut, "
    "shelter, or modular building) even if it never says so.\n\n"
    "Classify opportunity_type as:\n"
    '  "direct"  = explicitly seeking/spec\'ing/sourcing a hut, shelter, or '
    "modular data-center building;\n"
    '  "related" = a utility/co-op/ISP/carrier/agency expanding infrastructure '
    "that likely needs such a structure soon;\n"
    '  "none"    = not a real opportunity.\n'
    "Also judge geography: is the opportunity in the United States? "
    "(is_usa true/false; use false only when clearly non-U.S.)\n\n"
    'Respond ONLY with JSON: {"relevant": bool, "opportunity_type": '
    '"direct"|"related"|"none", "is_usa": bool, "confidence": 0-1 float, '
    '"one_line_summary": string}. The summary must say WHY it is (or is not) an '
    "opportunity in one line."
)


def classify_post(
    title: str, body: str, forum_name: str, context: str = ""
) -> Classification | None:
    """Classify a post's opportunity via Claude. Returns None if AI unavailable/fails.

    ``context`` is the operator's editable business/"intuition" context.
    """
    if not ai_available():
        return None
    try:
        client = _client()
        ctx = context.strip() or "(no extra context provided)"
        prompt = (
            f"BUSINESS CONTEXT / INTUITION:\n{ctx}\n\n"
            f"POST\nForum: {forum_name}\nTitle: {title}\n\nBody:\n{body[:2500]}\n\n"
            "Analyze this post."
        )
        resp = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=400,
            system=CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        data = _extract_json(text)
        if data is None:
            return None
        opp = str(data.get("opportunity_type", "")).strip().lower()
        if opp not in ("direct", "related", "none"):
            opp = ""
        is_usa = data.get("is_usa")
        return Classification(
            relevant=bool(data.get("relevant", False)),
            confidence=float(data.get("confidence", 0.0)),
            one_line_summary=str(data.get("one_line_summary", "")).strip(),
            opportunity_type=opp,
            is_usa=bool(is_usa) if isinstance(is_usa, bool) else None,
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
    context: str = "",
) -> str:
    """Generate a reply draft. AI if available, else a helpful manual template."""
    if not ai_available():
        return _manual_template(title, forum_name)
    try:
        client = _client()
        notes = f"\nForum posting rules to respect: {posting_notes}" if posting_notes else ""
        extra = f"\nOperator guidance: {guidance}" if guidance else ""
        ctx = f"\nBusiness context: {context.strip()}" if context.strip() else ""
        prompt = (
            f"Forum: {forum_name}{notes}{ctx}{extra}\n\n"
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
