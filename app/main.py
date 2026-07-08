"""FastAPI app: dashboard, forums, settings/accounts, and the reply workflow.

Run:   python -m app.main            (starts the server on :8420)
       python -m app.main --generate-key   (print a new FORUMAGENT_KEY)
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import config, credentials, drafting, scanner, scheduler
from .db import (
    Credential,
    Forum,
    Keyword,
    Post,
    Reply,
    get_setting,
    init_db,
    load_runtime_settings,
    seed_all,
    session,
    set_setting,
    utcnow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("forumagent")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

CATEGORIES = config.CATEGORIES  # TOWER/FIBER/DATA/E911/TRADES
BANDS = ["HIGH", "MEDIUM", "LOW"]
STATUSES = ["new", "reviewed", "lead", "ignored"]
GEOS = ["USA", "NON_USA", "UNKNOWN"]
OPPORTUNITIES = ["direct", "related", "none"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_data_dir()
    seed_all()
    scheduler.start_scheduler()
    logger.info("ForumAgent ready on http://%s:%s", config.HOST, config.PORT)
    yield
    scheduler.shutdown()


app = FastAPI(title="ForumAgent — CellSite Solutions", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- Template helpers -------------------------------------------------------


def _highlight(excerpt: str, matched: list[dict]) -> str:
    """Wrap matched keywords in <mark> for display. Escapes first."""
    import html
    import re

    text = html.escape(excerpt or "")
    for m in sorted(matched, key=lambda x: -len(x["term"])):
        term = re.escape(html.escape(m["term"]))
        pattern = re.compile(rf"(?<![A-Za-z0-9])({term}(?:es|s)?)(?![A-Za-z0-9])", re.IGNORECASE)
        text = pattern.sub(r"<mark>\1</mark>", text)
    return text


templates.env.filters["highlight"] = _highlight


def _band_class(band: str) -> str:
    return {"HIGH": "band-high", "MEDIUM": "band-medium", "LOW": "band-low"}.get(band, "band-low")


templates.env.filters["band_class"] = _band_class


def _dt(value) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M")


templates.env.filters["dt"] = _dt


def _base_context(request: Request, db) -> dict:
    settings = load_runtime_settings(db)
    return {
        "request": request,
        "ai_available": drafting.ai_available(),
        "credentials_available": credentials.credentials_available(),
        "settings": settings,
        "categories": CATEGORIES,
        "bands": BANDS,
        "statuses": STATUSES,
        "geos": GEOS,
        "opportunities": OPPORTUNITIES,
    }


# --- Dashboard --------------------------------------------------------------


def _query_posts(
    db, *, forum_slug="", category="", band="", status="", q="", competitor="",
    geo="", opportunity="", limit=200,
):
    stmt = select(Post).join(Forum)
    if forum_slug:
        stmt = stmt.where(Forum.slug == forum_slug)
    if category:
        # Category tabs are POST-topic based (from matched keyword categories).
        stmt = stmt.where(Post.topics_json.ilike(f'%"{category}"%'))
    if band:
        stmt = stmt.where(Post.score_band == band)
    if status:
        stmt = stmt.where(Post.status == status)
    if competitor:
        stmt = stmt.where(Post.matched_competitors_json.ilike(f'%"{competitor}"%'))
    if geo:
        stmt = stmt.where(Post.geo == geo)
    if opportunity:
        stmt = stmt.where(Post.opportunity_type == opportunity)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Post.title.ilike(like)) | (Post.body_excerpt.ilike(like)))
    stmt = stmt.order_by(Post.score.desc(), Post.posted_at.desc()).limit(limit)
    return db.scalars(stmt).all()


# TRADES intentionally omitted from the stat tabs (still a valid topic/filter).
STAT_CATEGORIES = config.TAB_CATEGORIES  # TOWER/FIBER/DATA/E911


def _stats(db):
    settings = load_runtime_settings(db)
    since = utcnow() - timedelta(days=settings.lookback_days)
    total = db.scalar(select(func.count(Post.id)).where(Post.posted_at >= since)) or 0
    # posted_at can be null for some sources; also count those via fetched_at fallback.
    total_all = db.scalar(select(func.count(Post.id))) or 0
    by_cat = {}
    for cat in STAT_CATEGORIES:
        by_cat[cat] = db.scalar(
            select(func.count(Post.id)).where(Post.topics_json.ilike(f'%"{cat}"%'))
        ) or 0
    leads = db.scalar(select(func.count(Post.id)).where(Post.status == "lead")) or 0
    high = db.scalar(select(func.count(Post.id)).where(Post.score_band == "HIGH")) or 0
    non_usa = db.scalar(select(func.count(Post.id)).where(Post.geo == "NON_USA")) or 0
    by_opp = {
        opp: db.scalar(select(func.count(Post.id)).where(Post.opportunity_type == opp)) or 0
        for opp in ("direct", "related")
    }

    # Competitive references: count posts mentioning each tracked competitor.
    competitors = config.load_competitors()
    comp_counts = {
        name: db.scalar(
            select(func.count(Post.id)).where(Post.matched_competitors_json.ilike(f'%"{name}"%'))
        ) or 0
        for name in competitors
    }
    return {
        "total_recent": total,
        "total_all": total_all,
        "by_category": by_cat,
        "leads": leads,
        "high": high,
        "non_usa": non_usa,
        "by_opportunity": by_opp,
        "competitors": comp_counts,
    }


def _last_scan_at(db) -> datetime | None:
    raw = get_setting(db, "last_scan_at", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    forum: str = "",
    category: str = "",
    band: str = "",
    status: str = "",
    q: str = "",
    competitor: str = "",
    geo: str = "",
    opportunity: str = "",
):
    with session() as db:
        ctx = _base_context(request, db)
        posts = _query_posts(
            db, forum_slug=forum, category=category, band=band, status=status, q=q,
            competitor=competitor, geo=geo, opportunity=opportunity,
        )
        forums = db.scalars(select(Forum).order_by(Forum.name)).all()
        ctx.update(
            {
                "posts": posts,
                "forums": forums,
                "stats": _stats(db),
                "status": scanner.scan_status(),
                "last_scan_at": _last_scan_at(db),
                "filters": {
                    "forum": forum, "category": category, "band": band,
                    "status": status, "q": q, "competitor": competitor,
                    "geo": geo, "opportunity": opportunity,
                },
            }
        )
        return templates.TemplateResponse("dashboard.html", ctx)


@app.get("/partials/posts", response_class=HTMLResponse)
def partial_posts(
    request: Request,
    forum: str = "",
    category: str = "",
    band: str = "",
    status: str = "",
    q: str = "",
    competitor: str = "",
    geo: str = "",
    opportunity: str = "",
):
    with session() as db:
        posts = _query_posts(
            db, forum_slug=forum, category=category, band=band, status=status, q=q,
            competitor=competitor, geo=geo, opportunity=opportunity,
        )
        return templates.TemplateResponse(
            "partials/post_cards.html", {"request": request, "posts": posts, "ai_available": drafting.ai_available()}
        )


def _compute_since(db, mode: str, days: int) -> datetime | None:
    """Resolve the lookback start for a Scan Now request.

    - ``days``: explicit last-N-days window.
    - ``since_last``: incremental — since the last completed scan; falls back to
      the full lookback window on the very first scan.
    """
    settings = load_runtime_settings(db)
    if mode == "days" and days > 0:
        return utcnow() - timedelta(days=days)
    last = _last_scan_at(db)
    if last is not None:
        return last
    return utcnow() - timedelta(days=settings.lookback_days)


@app.post("/scan", response_class=HTMLResponse)
def scan_now(request: Request, mode: str = Form("since_last"), days: int = Form(0)):
    with session() as db:
        since = _compute_since(db, mode, days)
    started = scanner.start_scan(since=since)
    logger.info("Scan requested (mode=%s, since=%s): started=%s", mode, since, started)
    # HTMX request (dashboard panel) -> return the live status; the scan runs in
    # the background so the browser never blocks. Plain form (header button) -> redirect.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/scan_status.html", {"request": request, "status": scanner.scan_status()}
        )
    return RedirectResponse(url="/", status_code=303)


@app.post("/reanalyze", response_class=HTMLResponse)
def reanalyze(request: Request):
    started = scanner.start_reanalyze()
    logger.info("Re-analyze requested: started=%s", started)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/scan_status.html", {"request": request, "status": scanner.scan_status()}
        )
    return RedirectResponse(url="/", status_code=303)


@app.get("/partials/scan-status", response_class=HTMLResponse)
def scan_status_partial(request: Request):
    return templates.TemplateResponse(
        "partials/scan_status.html", {"request": request, "status": scanner.scan_status()}
    )


@app.post("/posts/{post_id}/status", response_class=HTMLResponse)
def set_post_status(request: Request, post_id: int, status: str = Form(...)):
    with session() as db:
        post = db.get(Post, post_id)
        if post and status in STATUSES:
            post.status = status
            db.commit()
        db.refresh(post) if post else None
        return templates.TemplateResponse(
            "partials/post_card.html",
            {"request": request, "post": post, "ai_available": drafting.ai_available()},
        )


# --- Forums page ------------------------------------------------------------


@app.get("/forums", response_class=HTMLResponse)
def forums_page(request: Request):
    with session() as db:
        ctx = _base_context(request, db)
        forums = db.scalars(select(Forum).order_by(Forum.viability, Forum.name)).all()
        counts = {
            f.id: db.scalar(select(func.count(Post.id)).where(Post.forum_id == f.id)) or 0
            for f in forums
        }
        ctx.update({"forums": forums, "post_counts": counts})
        return templates.TemplateResponse("forums.html", ctx)


@app.post("/forums/{forum_id}/toggle")
def toggle_forum(forum_id: int):
    with session() as db:
        forum = db.get(Forum, forum_id)
        if forum:
            forum.enabled = not forum.enabled
            db.commit()
    return RedirectResponse(url="/forums", status_code=303)


@app.post("/forums/{forum_id}/scan")
def scan_forum_now(forum_id: int):
    scanner.scan_one(forum_id)
    return RedirectResponse(url="/forums", status_code=303)


# --- Settings ---------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with session() as db:
        ctx = _base_context(request, db)
        keywords = db.scalars(
            select(Keyword).order_by(Keyword.is_competitor, Keyword.is_booster, Keyword.weight.desc())
        ).all()
        ctx.update(
            {
                "keywords": keywords,
                "ai_context": get_setting(db, "ai_context", config.DEFAULT_AI_CONTEXT),
                "usa_only": get_setting(db, "usa_only", "1") == "1",
                "ai_scope": get_setting(db, "ai_scope", "matched"),
            }
        )
        return templates.TemplateResponse("settings.html", ctx)


@app.post("/settings")
def save_settings(
    lookback_days: int = Form(...),
    scan_interval_hours: int = Form(...),
    recency_half_life_days: int = Form(...),
    threshold_high: float = Form(...),
    threshold_medium: float = Form(...),
    usa_only: str = Form(""),
):
    with session() as db:
        set_setting(db, "lookback_days", str(lookback_days))
        set_setting(db, "scan_interval_hours", str(scan_interval_hours))
        set_setting(db, "recency_half_life_days", str(recency_half_life_days))
        set_setting(db, "threshold_high", str(threshold_high))
        set_setting(db, "threshold_medium", str(threshold_medium))
        set_setting(db, "usa_only", "1" if usa_only else "0")
    scheduler.reschedule(scan_interval_hours)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/context")
def save_context(ai_context: str = Form(""), ai_scope: str = Form("matched")):
    with session() as db:
        set_setting(db, "ai_context", ai_context.strip())
        if ai_scope in ("matched", "medium_plus", "all"):
            set_setting(db, "ai_scope", ai_scope)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/keywords/add")
def add_keyword(
    term: str = Form(...),
    weight: float = Form(1.0),
    category: str = Form(""),
    kind: str = Form("keyword"),
):
    with session() as db:
        term = term.strip()
        if term and not db.scalar(select(Keyword).where(Keyword.term == term)):
            db.add(
                Keyword(
                    term=term,
                    weight=weight,
                    category="COMPETITOR" if kind == "competitor" else category,
                    is_booster=(kind == "booster"),
                    is_competitor=(kind == "competitor"),
                )
            )
            db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/keywords/{keyword_id}/update")
def update_keyword(keyword_id: int, weight: float = Form(...), enabled: str = Form("")):
    with session() as db:
        kw = db.get(Keyword, keyword_id)
        if kw:
            kw.weight = weight
            kw.enabled = bool(enabled)
            db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/keywords/{keyword_id}/delete")
def delete_keyword(keyword_id: int):
    with session() as db:
        kw = db.get(Keyword, keyword_id)
        if kw:
            db.delete(kw)
            db.commit()
    return RedirectResponse(url="/settings", status_code=303)


# --- Accounts (Phase 2) -----------------------------------------------------


def _masked_credential_view(db):
    """Return display-safe views of stored credentials keyed by kind/forum."""
    out = {"reddit": None, "forums": {}}
    reddit = db.scalar(select(Credential).where(Credential.kind == "reddit_oauth"))
    if reddit:
        try:
            data = credentials.get_credential_data(db, reddit)
            out["reddit"] = {
                "id": reddit.id,
                "masked": credentials.masked_summary(data),
                "verify_status": reddit.verify_status,
                "last_verified_at": reddit.last_verified_at,
            }
        except ValueError:
            out["reddit"] = {"id": reddit.id, "masked": {}, "verify_status": "decrypt error", "last_verified_at": None}
    for cred in db.scalars(select(Credential).where(Credential.forum_id.is_not(None))).all():
        try:
            data = credentials.get_credential_data(db, cred)
            masked = credentials.masked_summary(data)
        except ValueError:
            masked = {}
        out["forums"][cred.forum_id] = {
            "id": cred.id,
            "kind": cred.kind,
            "masked": masked,
            "verify_status": cred.verify_status,
            "last_verified_at": cred.last_verified_at,
        }
    return out


@app.get("/settings/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    with session() as db:
        ctx = _base_context(request, db)
        # Forums that support credential entry: reddit (global) + discourse (per-forum).
        discourse_forums = db.scalars(
            select(Forum).where(Forum.adapter_type == "discourse").order_by(Forum.name)
        ).all()
        ctx.update(
            {
                "creds": _masked_credential_view(db),
                "discourse_forums": discourse_forums,
                "master_key_set": credentials.credentials_available(),
            }
        )
        return templates.TemplateResponse("accounts.html", ctx)


@app.post("/accounts/reddit")
def save_reddit(
    client_id: str = Form(...),
    client_secret: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    user_agent: str = Form(""),
):
    with session() as db:
        data = {
            "client_id": client_id.strip(),
            "client_secret": client_secret.strip(),
            "username": username.strip(),
            "password": password,
            "user_agent": user_agent.strip() or f"ForumAgent/1.0 by u/{username.strip()}",
        }
        credentials.save_credential(db, forum_id=None, kind="reddit_oauth", data=data)
    return RedirectResponse(url="/settings/accounts", status_code=303)


@app.post("/accounts/reddit/verify")
def verify_reddit_creds():
    with session() as db:
        cred = db.scalar(select(Credential).where(Credential.kind == "reddit_oauth"))
        if cred:
            try:
                data = credentials.get_credential_data(db, cred)
                ok, msg = credentials.verify_reddit(data)
                # Karma/age advisory.
                advisory = _reddit_age_karma_advisory(data)
                status = ("OK" if ok else "FAIL") + f" — {msg}" + (f" | {advisory}" if advisory else "")
                credentials.mark_verified(db, cred, status[:200])
            except ValueError as exc:
                credentials.mark_verified(db, cred, f"decrypt error: {exc}"[:200])
    return RedirectResponse(url="/settings/accounts", status_code=303)


def _reddit_age_karma_advisory(data) -> str:
    try:
        from .adapters.reddit import _make_reddit

        reddit = _make_reddit(data)
        me = reddit.user.me()
        if me is None:
            return ""
        age_days = (utcnow().timestamp() - me.created_utc) / 86400.0
        karma = (me.link_karma or 0) + (me.comment_karma or 0)
        warnings = []
        if age_days < 30:
            warnings.append(f"account is {int(age_days)}d old (<30d: age before posting)")
        if karma < 50:
            warnings.append(f"low karma ({karma})")
        return "; ".join(warnings)
    except Exception:  # noqa: BLE001
        return ""


@app.post("/accounts/forum/{forum_id}")
def save_forum_credential(
    forum_id: int,
    api_key: str = Form(""),
    api_username: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
):
    with session() as db:
        forum = db.get(Forum, forum_id)
        if forum is None:
            return RedirectResponse(url="/settings/accounts", status_code=303)
        if forum.adapter_type == "discourse":
            data = {
                "api_key": api_key.strip(),
                "api_username": api_username.strip(),
                "base_url": (forum.adapter_config.get("base_url") or forum.url).rstrip("/"),
            }
            credentials.save_credential(db, forum_id=forum_id, kind="discourse_api_key", data=data)
        else:
            data = {"username": username.strip(), "password": password}
            credentials.save_credential(db, forum_id=forum_id, kind="username_password", data=data)
    return RedirectResponse(url="/settings/accounts", status_code=303)


@app.post("/accounts/{cred_id}/verify")
def verify_credential(cred_id: int):
    with session() as db:
        cred = db.get(Credential, cred_id)
        if cred:
            try:
                data = credentials.get_credential_data(db, cred)
                if cred.kind == "discourse_api_key":
                    ok, msg = credentials.verify_discourse(data)
                elif cred.kind == "reddit_oauth":
                    ok, msg = credentials.verify_reddit(data)
                else:
                    ok, msg = False, "No verifier for username/password forums."
                credentials.mark_verified(db, cred, ("OK" if ok else "FAIL") + f" — {msg}"[:200])
            except ValueError as exc:
                credentials.mark_verified(db, cred, f"decrypt error: {exc}"[:200])
    return RedirectResponse(url="/settings/accounts", status_code=303)


# --- Reply workflow ---------------------------------------------------------


def _get_or_create_reply(db, post_id: int) -> Reply:
    reply = db.scalar(
        select(Reply).where(Reply.post_id == post_id).order_by(Reply.id.desc())
    )
    if reply is None:
        reply = Reply(post_id=post_id, status="draft")
        db.add(reply)
        db.commit()
        db.refresh(reply)
    return reply


@app.get("/posts/{post_id}/reply", response_class=HTMLResponse)
def reply_page(request: Request, post_id: int):
    with session() as db:
        ctx = _base_context(request, db)
        post = db.get(Post, post_id)
        if post is None:
            return RedirectResponse(url="/", status_code=303)
        forum = db.get(Forum, post.forum_id)
        reply = _get_or_create_reply(db, post_id)
        # Posting capability by adapter type.
        programmatic = forum.adapter_type in ("reddit", "discourse")
        ctx.update(
            {
                "post": post,
                "forum": forum,
                "reply": reply,
                "programmatic_posting": programmatic and forum.can_post,
                "assisted_only": not (programmatic and forum.can_post),
            }
        )
        return templates.TemplateResponse("reply.html", ctx)


@app.post("/posts/{post_id}/reply/ai-draft", response_class=HTMLResponse)
def ai_draft(request: Request, post_id: int, guidance: str = Form("")):
    with session() as db:
        post = db.get(Post, post_id)
        forum = db.get(Forum, post.forum_id)
        ai_context = get_setting(db, "ai_context", config.DEFAULT_AI_CONTEXT)
        draft = drafting.draft_reply(
            post.title, post.body_excerpt, forum.name, forum.posting_notes, guidance, ai_context
        )
        reply = _get_or_create_reply(db, post_id)
        reply.draft_body = draft
        reply.status = "draft"
        db.commit()
        return templates.TemplateResponse(
            "partials/draft_body.html", {"request": request, "reply": reply}
        )


@app.post("/replies/{reply_id}/save")
def save_reply(reply_id: int, body: str = Form(...)):
    with session() as db:
        reply = db.get(Reply, reply_id)
        if reply:
            reply.draft_body = body
            reply.final_body = body
            if reply.status not in ("posted", "manual_copied"):
                reply.status = "draft"
            db.commit()
        post_id = reply.post_id if reply else 0
    return RedirectResponse(url=f"/posts/{post_id}/reply", status_code=303)


@app.post("/replies/{reply_id}/approve")
def approve_reply(reply_id: int, body: str = Form(...)):
    with session() as db:
        reply = db.get(Reply, reply_id)
        if reply:
            reply.draft_body = body
            reply.final_body = body
            reply.status = "approved"
            db.commit()
        post_id = reply.post_id if reply else 0
    return RedirectResponse(url=f"/posts/{post_id}/reply", status_code=303)


@app.post("/replies/{reply_id}/post")
def post_reply_action(reply_id: int):
    """Post an APPROVED reply via the forum's adapter. Never auto-approves."""
    with session() as db:
        reply = db.get(Reply, reply_id)
        if reply is None:
            return RedirectResponse(url="/", status_code=303)
        post = db.get(Post, reply.post_id)
        forum = db.get(Forum, post.forum_id)
        if reply.status != "approved":
            reply.error = "Reply must be approved before posting."
            db.commit()
            return RedirectResponse(url=f"/posts/{post.id}/reply", status_code=303)

        from .adapters import build_adapter

        creds: dict = {}
        if forum.adapter_type == "reddit":
            creds = credentials.get_reddit_credentials(db) or {}
        elif forum.adapter_type == "discourse":
            pair = credentials.get_forum_credentials(db, forum.id)
            creds = pair[1] if pair else {}

        adapter = build_adapter(forum, credentials=creds if forum.adapter_type == "reddit" else None)
        result = adapter.post_reply(post, reply.final_body or reply.draft_body, creds)
        if result.ok:
            reply.status = "posted"
            reply.posted_url = result.url
            reply.posted_at = utcnow()
            reply.error = ""
            post.status = "lead"
        else:
            reply.status = "failed"
            reply.error = result.error
        db.commit()
        return RedirectResponse(url=f"/posts/{post.id}/reply", status_code=303)


@app.post("/replies/{reply_id}/mark-copied")
def mark_copied(reply_id: int):
    """Assisted flow: operator pasted the draft manually and confirms it."""
    with session() as db:
        reply = db.get(Reply, reply_id)
        if reply:
            reply.status = "manual_copied"
            reply.posted_at = utcnow()
            post = db.get(Post, reply.post_id)
            if post:
                post.status = "lead"
            db.commit()
        post_id = reply.post_id if reply else 0
    return RedirectResponse(url=f"/posts/{post_id}/reply", status_code=303)


# --- CLI entrypoint ---------------------------------------------------------


def _main() -> None:
    if "--generate-key" in sys.argv:
        print(credentials.generate_key())
        print(
            "\nAdd this to your .env as FORUMAGENT_KEY=... and keep it safe. "
            "Losing it means re-entering every stored credential.",
            file=sys.stderr,
        )
        return
    if "--seed" in sys.argv:
        seed_all()
        print("Seeded forums + keywords.")
        return

    import uvicorn

    init_db()
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    _main()
