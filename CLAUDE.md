# CLAUDE.md — ForumAgent

Read PROJECT_BRIEF.md first. It is the source of truth for scope and architecture.

## Build order
1. Phase 1 (monitoring) end-to-end before any Phase 2 code: db.py → adapters (rss +
   discourse first, they need no credentials) → scoring.py → dashboard → scheduler →
   reddit adapter → scrape adapter.
2. Phase 2 (credentials + posting) only after a global scan populates the dashboard.

## Hard rules
- Never invent forums or keywords — seed only from config/forums.json and
  config/keywords.json. If a site turns out to be dead or unscrapable, set
  `enabled: false` and record why in `notes`; do not silently drop it.
- No auto-posting. Every outbound reply requires an explicit human approve action.
- No LinkedIn scraping or LinkedIn posting automation of any kind (assisted
  copy-to-clipboard workflow only).
- Respect robots.txt; honest User-Agent; ≥5s between requests per host; backoff on
  429/5xx.
- Credentials: Fernet-encrypted at rest, decrypted only in memory, masked in UI, never
  logged, never committed. `data/` and `.env` are gitignored.
- The app must run fully without ANTHROPIC_API_KEY (keyword scoring + manual drafting
  as fallback).

## Conventions
- Python 3.11+, type hints throughout, ruff-clean.
- SQLAlchemy 2.x style (Mapped/mapped_column).
- Small focused modules; adapters share the base.py interface exactly.
- HTMX partial-render endpoints under /partials/*; keep JS minimal.
- Config seeding is idempotent — re-running never duplicates forums/keywords.

## Testing
- pytest; fixture HTML files under tests/fixtures/ for scrape-adapter parsing.
- Unit-test scoring math and dedupe before building the UI around them.

## When network access is available during the build
- Verify RSS feed URLs actually resolve before hard-coding them.
- Verify telecomhall.net Discourse endpoints (/latest.json) respond.
- Check whether DSLReports forums are still live (they may be read-only/defunct);
  disable with a note if so.
