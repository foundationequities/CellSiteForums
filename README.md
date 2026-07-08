# ForumAgent — CellSite Solutions

A locally-run web app that monitors 28 telecom / fiber / data-center communities for
posts about shelters, huts, cabinets, and modular data centers, scores them for sales
relevance, and helps you draft **human-approved** replies. Built to the spec in
[`PROJECT_BRIEF.md`](PROJECT_BRIEF.md).

- **Phase 1 — Monitor:** scan RSS, Discourse, Reddit, and scrape sources for the last
  60 days, score by keyword relevance, and review on a dashboard.
- **Phase 2 — Engage:** store per-forum credentials (encrypted), draft replies
  (manual or AI-assisted), approve, and post — Reddit + Discourse programmatically,
  everything else via assisted copy-to-clipboard. **No auto-posting, ever.**

Runs entirely on `localhost`, single operator, SQLite. Works fully **without** any AI
key (keyword scoring + manual drafting fallback).

---

## Quick start

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. (Phase 2 only) generate a master key for credential encryption
python -m app.main --generate-key
#   -> copy the printed key into a .env file (see .env.example):
#      FORUMAGENT_KEY=<the key>

# 3. Run
python -m app.main
#   -> http://localhost:8420   (28 forums seeded on first run)
```

Then open the dashboard and click **Scan Now**. With no credentials configured, the scan
still pulls the Discourse, RSS, and scrape sources and fills the dashboard. Add Reddit
credentials (below) to unlock the 12 subreddits.

> **First-run note.** The database (`data/forumagent.db`), logs, and `.env` are all
> gitignored. Deleting `data/` resets everything; re-running reseeds the 28 forums and
> keywords idempotently.

---

## First-run checklist

1. `pip install -r requirements.txt`
2. `python -m app.main --generate-key` → save `FORUMAGENT_KEY` in `.env`
   (skip if you only want Phase 1 monitoring for now).
3. `python -m app.main`, open http://localhost:8420, click **Scan Now**.
4. Review scored posts; mark **Lead / Reviewed / Ignore**; open **Draft Reply** on promising ones.
5. (Phase 2) Add credentials under **Accounts**, then use the reply workflow.

---

## Reddit API setup (unlocks the 12 subreddits)

Reddit monitoring/posting uses the **official API** via a *script* app — no scraping.

1. Log in as the Reddit account you'll post from (age it ~30 days and earn some karma
   before posting — the app warns you if it's newer/low-karma).
2. Go to **https://www.reddit.com/prefs/apps** → **Create another app…**
3. Choose **script**. Set:
   - **name:** `ForumAgent`
   - **redirect uri:** `http://localhost:8420` (unused for script apps, but required)
4. After creating, note:
   - **client id** — the string just under the app name (“personal use script”)
   - **client secret** — the `secret` field
5. In ForumAgent: **Accounts → Reddit**, enter client id, client secret, the account
   **username** and **password**, then **Save** and **Verify** (fetches your identity).
6. Run a scan — the 12 subreddits now populate. PRAW handles Reddit's rate limits.

Posting to Reddit is done from a post's **Draft Reply** page: write or AI-draft →
**Approve** → **Post**. Approval is always required; nothing posts automatically.

---

## Optional: Anthropic API (AI classification + drafting)

The app runs fully without it. If you set `ANTHROPIC_API_KEY` in `.env`:

- Posts scoring **MEDIUM or higher** are classified by Claude (`is this a genuine B2B
  infrastructure procurement/spec discussion?`) and get a one-line summary on the card.
- The **Draft Reply** page gains a **Generate AI draft** button that answers the poster's
  question first and mentions CellSite Solutions at most once, only when relevant.

```
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

Models used: `claude-haiku-4-5` for classification, `claude-sonnet-5` for drafting.

### Turning on AI and applying it to posts you already pulled

1. **Get a key** at <https://console.anthropic.com> → *API Keys* → *Create key*.
2. **Add it to `.env`** in the project root (same file as `FORUMAGENT_KEY`):
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   `.env` is gitignored — the key is never committed.
3. **Restart the app** so it re-reads `.env`:
   ```
   # stop the running server (Ctrl-C), then:
   source .venv/bin/activate
   python -m app.main
   ```
   The header should now show an **“AI on”** pill.
4. **Apply AI to the posts already in your dashboard.** A normal *Scan Now*
   only analyzes **new** posts (it dedupes and skips ones already stored), so to
   run AI over what you've already collected click **“Re-analyze with AI”** on
   the dashboard scan panel. It re-scores and re-classifies every stored post in
   the background — **without re-fetching the forums** — and **keeps your
   Lead/Reviewed/Ignore marks**. When it finishes, the list refreshes with AI
   summaries, opportunity types, and any rank changes.
5. **Future scans** then classify new posts automatically as they arrive.

Tip: **Settings → AI intuition & context** has an *AI analysis scope* selector.
The default **“Matched”** runs AI on any post that hit a keyword/competitor —
this is what lets AI catch latent leads (e.g. a fiber-footprint expansion that
barely matches keywords) and lift them up the list. Choose **“Medium+ only”** to
minimize API calls, or **“All fetched posts”** for maximum coverage.

---

## Discourse (Telecom Hall) posting

Reading Telecom Hall needs **no credentials** (public JSON API). To *post*:

1. In your Telecom Hall account, generate a **User API Key** (or have an admin issue one).
2. ForumAgent: **Accounts → Discourse**, enter the **API key** and your **API username**,
   **Save**, then **Verify**.
3. Reply workflow → **Approve** → **Post** submits via `POST /posts.json`.

---

## Assisted posting (scrape + LinkedIn forums)

ContractorTalk, Broadband Forum, DSLReports, Spiceworks, and all LinkedIn groups use
**assisted posting only** — no automated form submission (per-forum ToS vary and
LinkedIn prohibits automation). The reply page's **Copy & open thread** button copies
your approved draft to the clipboard and opens the thread; you paste and post from your
own logged-in browser, then click **Mark as manually posted** for tracking.

---

## How scoring works

```
score = Σ(keyword weight × occurrences, capped at 3 per keyword)
      + title-match bonus (title occurrences count double)
      + buying-signal booster (+3 if any of RFP/RFQ/quote/vendor/… co-occurs with a match)
then × viability multiplier (STRONG 1.3 / GOOD 1.1 / MODERATE 1.0)
then × recency decay (exponential, half-life 21 days by default)

Bands: HIGH ≥ 12, MEDIUM ≥ 6, else LOW   (all thresholds tunable in Settings)
```

Matching is case-insensitive, word-boundary aware, with simple plural handling. Keywords
and weights seed from `keywords.json` and are editable in **Settings** (persisted to the DB).

---

## Opportunity intelligence (USA focus, intuition context, E911)

Beyond raw keyword scoring, the agent adds context:

- **Post topics as tabs.** Every post is tagged with topics derived from the categories of
  the keywords it matched — **TOWER / FIBER / DATA / E911** show as clickable stat tabs
  (TRADES stays a valid topic/filter). Tabs reflect what a *post* is about, not just which
  forum it came from. E911/NG911 dispatch-center and PSAP terms are seeded in
  `keywords.json`.

- **USA-only focus.** Each post gets a geography tag (`USA` / `NON_USA` / `UNKNOWN`). With
  **USA only** enabled in Settings (default on), posts identified as non-U.S. are heavily
  down-weighted, badged **non-US**, and sink to the bottom — they stay visible for
  reference but out of your way. Detection uses a keyword geo-heuristic (US states, FCC,
  BEAD, RDOF … vs Ofcom, Openreach, NBN, £/€ …), upgraded to an AI judgment when a key is set.

- **Intuition context (Settings → AI intuition & context).** A free-text box describing your
  ideal opportunity. When `ANTHROPIC_API_KEY` is set, this steers the AI to read each post
  for intent and classify **opportunity type**:
  - **direct** — explicitly seeking/spec'ing a fiber hut, telecom/equipment shelter, or
    modular data-center building;
  - **related** — a utility, electric co-op, ISP, carrier, or agency expanding fiber /
    FTTH / middle-mile / small-cell / E911 infrastructure that will *likely need* a
    structure soon, even if not stated;
  - **none**.

  The **Direct opps** and **Related opps** stat tiles and the **Opportunity** filter surface
  these. Without an API key the app falls back to a heuristic (HIGH→direct, MEDIUM→related)
  and the geo-heuristic above, so it still runs fully offline of any AI.

The context is pre-seeded with CellSite's profile (fiber huts, telecom shelters, modular
buildings, latent demand from fiber/utility expansion and E911) — edit it any time to
re-focus what the agent treats as important. New context/settings apply to newly scanned
posts, so re-scan after changing them.

---

## Adding or changing a forum

Forums seed from **`forums.json`** (repo root). Each entry:

```json
{
  "slug": "unique-slug",
  "name": "Display Name",
  "url": "https://…",
  "category": "TOWER | FIBER | DATA | TRADES",
  "viability": "STRONG | GOOD | MODERATE",
  "adapter_type": "rss | discourse | reddit | scrape | manual",
  "adapter_config": { },
  "can_post": true,
  "enabled": true,
  "posting_notes": "self-promotion rules shown before posting",
  "notes": "why this forum is on the list"
}
```

Adapter-config highlights:
- **rss:** optional `feed_urls` (list). If omitted, the adapter auto-discovers the feed
  from the site and falls back to conventional paths — so it keeps working without
  hardcoded URLs. Verify the feed resolves on your machine and pin `feed_urls` if needed.
- **discourse:** `base_url`; optional `search_terms` to also pull keyword hits.
- **reddit:** `subreddit`; optional `require_keyword_match`, `search_terms`, `new_limit`.
- **scrape:** optional `listing_urls`, `item_selector`, `title_selector`, `link_selector`,
  `date_selector`, `date_format`, `base_url`; `require_keyword_match` to drop non-matches.
- **manual:** no fetching, assisted posting only.

Re-running the app reseeds idempotently: existing forums are refreshed from the file but
your **enabled** toggles are preserved. Never delete a dead forum — set `enabled: false`
and say why in `notes`.

> **Build-time note on endpoints.** This build environment had restricted outbound
> network access, so external feed/forum URLs could **not** be verified here. On your
> machine (open network), run a scan and check **Forums → Last scan** for each source;
> the RSS adapter auto-discovers feeds, and scrape adapters honor `robots.txt`. If a site
> is dead or read-only (e.g. DSLReports may be), disable it with a note rather than
> leaving it failing.

---

## Compliance guardrails (baked in)

- **No fully automated posting** — every outbound reply requires an explicit human
  Approve step.
- **Polite scraping** — honest User-Agent
  (`ForumAgent/1.0 (CellSite Solutions; contact@cellsitesolutions.com)`), `robots.txt`
  respected, ≥ 5 s between requests per host, exponential backoff on 429/5xx.
- **LinkedIn** — read + post are manual-only via the assisted workflow; no scraping,
  no automation.
- **Reddit** — official API only; the drafter is biased toward genuinely helpful answers.
- **Credentials** — Fernet-encrypted at rest, decrypted only in memory, masked in the UI,
  never logged, never committed.
- Every forum shows its **posting_notes** (self-promotion rules) next to the editor.

---

## Project layout

```
app/
  main.py            FastAPI app + routes
  scheduler.py       APScheduler (default every 6 h) + manual Scan Now
  scanner.py         fetch → dedupe → score → optional AI → persist
  db.py              SQLAlchemy 2.x models + idempotent seeding
  scoring.py         keyword relevance scoring (pure, unit-tested)
  drafting.py        reply drafts + optional Claude classifier/drafter
  credentials.py     Fernet-encrypted credential store
  config.py          paths, settings, config-file loading
  adapters/          base.py, rss.py, discourse.py, reddit.py, scrape.py, manual.py
  templates/         Jinja2 + HTMX
  static/            style.css
forums.json          28-forum build list (do not invent forums)
keywords.json        keyword taxonomy + buying-signal boosters
tests/               scoring, dedupe/seed, scrape-parse (fixture HTML)
```

## Running the tests

```bash
source .venv/bin/activate
pytest -q          # 17 tests: scoring math, dedupe/seed idempotency, scrape parse
ruff check app tests
```
