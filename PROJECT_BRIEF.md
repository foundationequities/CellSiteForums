# CellSite Solutions ForumAgent — Project Brief

## What this is

A locally-run web application for CellSite Solutions (www.cellsitesolutions.com) that:

1. **Phase 1 — Monitor:** Scans 28 pre-configured forums/communities for posts from the
   last 60 days mentioning telecom shelters, fiber huts, equipment shelters, modular/edge
   data centers, and related procurement topics. Scores and highlights relevant posts in
   a dashboard.
2. **Phase 2 — Engage:** Lets the operator configure per-forum login credentials, draft
   replies (optionally AI-assisted), review them, and post to forums that support it —
   always human-in-the-loop, never fully automated.

Target user: one internal operator at CellSite Solutions. Runs on a single machine
(localhost). No multi-tenant concerns.

## Tech stack

- Python 3.11+
- FastAPI + Jinja2 templates + HTMX for the dashboard (single server, no separate SPA build)
- SQLite via SQLAlchemy 2.x (file: `data/forumagent.db`)
- httpx + BeautifulSoup4 + feedparser for fetching/parsing
- PRAW for Reddit (official API, OAuth script app)
- APScheduler for scheduled scans (default: every 6 hours; manual "Scan Now" button too)
- `cryptography` (Fernet) for credential encryption at rest; master key from an
  environment variable `FORUMAGENT_KEY` (generate on first run, tell the user to save it)
- Optional: Anthropic API for post relevance classification and reply drafting
  (`ANTHROPIC_API_KEY` env var; app must work fully without it — fall back to keyword
  scoring and manual drafting)

## Repository layout

```
forumagent/
  app/
    main.py              # FastAPI app + routes
    scheduler.py         # APScheduler setup
    db.py                # SQLAlchemy models + session
    scoring.py           # keyword relevance scoring (+ optional Claude classifier)
    drafting.py          # reply draft helpers (+ optional Claude drafting)
    credentials.py       # Fernet-encrypted credential store
    adapters/
      base.py            # Adapter interface: fetch_recent(since_dt) -> list[RawPost]; post_reply(...)
      reddit.py          # PRAW; monitors + posting
      discourse.py       # Discourse JSON API (telecomhall.net); monitors + posting
      rss.py             # RSS/Atom feeds (news/trade pubs); monitor only
      scrape.py          # Generic HTML forum scraper w/ per-forum CSS selectors; monitor; assisted posting
      manual.py          # LinkedIn groups etc. — no fetching; assisted posting only
    templates/           # Jinja2 + HTMX
    static/
  config/
    forums.json          # 28-forum build list (provided — do not invent forums)
    keywords.json        # keyword taxonomy with weights (provided)
  data/                  # SQLite db, logs (gitignored)
  tests/
  CLAUDE.md
  PROJECT_BRIEF.md
  requirements.txt
  README.md
```

## Data model (minimum)

- `Forum` — id, name, slug, url, category (TOWER/FIBER/DATA/TRADES), viability
  (STRONG/GOOD/MODERATE), adapter_type, adapter_config (JSON), enabled, can_post,
  posting_notes, last_scanned_at
- `Post` — id, forum_id, external_id (unique per forum), url, title, author, body_excerpt
  (first ~1500 chars), posted_at, fetched_at, score (float), matched_keywords (JSON),
  ai_summary (nullable), status (new / reviewed / lead / ignored)
- `Credential` — id, forum_id, kind (reddit_oauth / discourse_api_key / username_password),
  encrypted_blob, created_at, last_verified_at, verify_status
- `Reply` — id, post_id, draft_body, final_body, status (draft / approved / posted /
  failed / manual_copied), posted_url, posted_at, error

Deduplicate on (forum_id, external_id). Never re-alert on a post already stored.

## Phase 1 — Monitoring (build this first, end to end)

1. Load `config/forums.json` into the DB on first run (idempotent seed).
2. Each adapter implements `fetch_recent(since: datetime) -> list[RawPost]` with a
   60-day default lookback (configurable in Settings).
3. Adapter specifics:
   - **reddit.py** — one PRAW instance, iterate configured subreddits via `.new(limit=...)`
     and `.search()` on top keywords with `time_filter` fitting 60 days. Requires the
     user's Reddit app client_id/secret + username/password (script app) entered in
     Settings. Respect Reddit API rate limits (PRAW handles this).
   - **discourse.py** — telecomhall.net is Discourse. Use public JSON endpoints:
     `/latest.json`, `/search.json?q=<keyword>+after:<date>`. No auth needed for reading.
   - **rss.py** — for trade pubs (DataCenterDynamics, DataCenterKnowledge, RCR Wireless,
     Light Reading, FierceTelecom, Cabling Install, Wireless Estimator). Discover feed
     URLs at build time; if a feed is missing fall back to the scrape adapter on the
     site's article-listing page.
   - **scrape.py** — httpx + BeautifulSoup with per-forum config: listing URL(s), item
     selector, title/link/date selectors, date format. Politeness is mandatory: check
     robots.txt, identify with a real User-Agent string
     (`ForumAgent/1.0 (CellSite Solutions; contact@cellsitesolutions.com)`), max 1
     request per 5 seconds per host, cache pages, exponential backoff on 429/5xx.
     Used for Broadband Forum, ContractorTalk, DSLReports FTTH, Spiceworks. Verify each
     site is still live/scrapable during the build; if one is defunct or read-only
     (DSLReports may be), mark it `enabled: false` with a note rather than failing.
   - **manual.py** — LinkedIn groups. LinkedIn prohibits scraping and automated posting;
     do not fetch or post programmatically. These forums appear on the dashboard as
     "manual channels" with a link out and the draft-copy workflow only.
4. **Scoring** (`scoring.py`): score = Σ(keyword weight × occurrences, capped per keyword)
   + title-match bonus (×2) + forum-viability multiplier (STRONG 1.3 / GOOD 1.1 /
   MODERATE 1.0) + recency decay (half-life 21 days). Thresholds: HIGH ≥ 12, MEDIUM ≥ 6,
   else LOW (tune during build; make thresholds configurable).
   If `ANTHROPIC_API_KEY` is set, run posts scoring ≥ MEDIUM through Claude
   (claude-sonnet or haiku via the Messages API) with a prompt that classifies:
   is this a B2B infrastructure procurement/spec discussion relevant to a seller of
   telecom shelters, fiber huts, equipment shelters, and modular data centers? Return
   JSON {relevant: bool, confidence: 0-1, one_line_summary}. Store the summary.
5. **Dashboard** (`/`):
   - Feed of matched posts, newest first; filter by forum, category, score band, status;
     full-text search.
   - Each card: forum badge, score band (color), title (links to source), matched
     keywords highlighted in the excerpt, AI summary if present, status buttons
     (Reviewed / Lead / Ignore), and a "Draft Reply" button.
   - Stats bar: posts found last 60 days, by category, leads marked.
   - Forums page: per-forum status, last scan, post counts, enable/disable toggle,
     "Scan now" per forum and global.

## Phase 2 — Credentials & posting (build after Phase 1 works)

1. **Settings → Accounts:** per-forum credential forms. Store everything Fernet-encrypted
   in the `Credential` table; decrypt only in memory at use time. Mask values in the UI.
   Include a "Verify" button per credential (e.g., Reddit: fetch own identity;
   Discourse: GET /session/current.json or a cheap authed call).
2. **Reply workflow:** From a post card → Draft Reply page. Operator writes the reply, or
   clicks "AI draft" (only if API key set) which generates a helpful, non-spammy reply:
   answer the poster's actual question first, mention CellSite Solutions at most once
   with a link only when genuinely relevant, and match the forum's tone. Show the
   forum's `posting_notes` (rules) next to the editor. Statuses: draft → approved →
   posted. **Posting requires an explicit approve step; no auto-posting anywhere.**
3. **Posting adapters:**
   - Reddit: PRAW `submission.reply(...)` for comments. Surface subreddit self-promotion
     rules in `posting_notes`; warn if the account is < 30 days old or low karma
     (the assessment's own guidance: 30-day aging before posting).
   - Discourse (Telecom Hall): user-generated API key or username/password →
     POST /posts.json.
   - Scrape-type forums (ContractorTalk, Broadband Forum, etc.) and LinkedIn:
     **assisted posting** — button opens the thread URL in the browser and copies the
     approved draft to clipboard, then operator pastes manually and marks it
     "manual_copied". Do not implement headless-browser form submission; per-forum ToS
     vary and fragile automation creates ban risk.
4. Log every posted/manual_copied reply with URL + timestamp for follow-up tracking.

## Compliance guardrails (non-negotiable, bake into the app)

- No fully automated posting; every outbound reply has a human approve step.
- Respect robots.txt and rate limits on all scraping; identify honestly in User-Agent.
- LinkedIn: read + post are manual-only via the assisted workflow.
- Reddit: official API only, honest app registration, honor subreddit rules; the reply
  drafter must be biased toward genuinely helpful answers, not ad copy.
- A visible per-forum `posting_notes` field so the operator sees each community's
  self-promotion rules before posting.

## Keywords

Seed from `config/keywords.json`. Editable in Settings (add/remove/re-weight, persisted
to DB after first seed). Matching is case-insensitive, word-boundary aware, with simple
plural handling.

## Definition of done

- `pip install -r requirements.txt && python -m app.main` starts the app on
  http://localhost:8420 with the 28 forums seeded.
- With no credentials configured, a global scan still pulls Discourse + RSS + scrape
  sources and populates the dashboard.
- Adding Reddit credentials unlocks the 12 subreddits in one scan.
- Full reply workflow works end-to-end against Reddit (draft → approve → post) and the
  assisted-copy flow works for manual forums.
- README documents: first-run key generation, Reddit app setup steps, optional
  Anthropic key, and how to add a new forum to forums.json.
- Basic tests: scoring function, dedupe logic, one adapter parse test with fixture HTML.
