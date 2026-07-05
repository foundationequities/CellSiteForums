"""Reddit adapter via PRAW (official API, script app).

Reading: iterate the configured subreddit's ``.new()`` within the lookback
window, and optionally ``.search()`` on top keywords for noisy subs
(``require_keyword_match``/``search_terms`` in adapter_config).
Posting: ``submission.reply(...)`` — human-approved only, called from the
reply workflow. PRAW handles Reddit's rate limits.

Credentials (from the encrypted store) are a dict:
  {client_id, client_secret, username, password, user_agent?}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import Adapter, PostResult, RawPost

# 60-day lookback maps to Reddit's "month" search time_filter (closest fit).
SEARCH_TIME_FILTER = "month"
DEFAULT_NEW_LIMIT = 200


class RedditCredentialsMissing(RuntimeError):
    pass


def _make_reddit(credentials: dict[str, Any]):
    import praw  # imported lazily so the app runs without praw installed for non-reddit use

    required = ("client_id", "client_secret", "username", "password")
    missing = [k for k in required if not credentials.get(k)]
    if missing:
        raise RedditCredentialsMissing(f"Missing Reddit credential fields: {', '.join(missing)}")
    return praw.Reddit(
        client_id=credentials["client_id"],
        client_secret=credentials["client_secret"],
        username=credentials["username"],
        password=credentials["password"],
        user_agent=credentials.get(
            "user_agent", "ForumAgent/1.0 by CellSite Solutions"
        ),
        check_for_async=False,
    )


class RedditAdapter(Adapter):
    adapter_type = "reddit"

    def __init__(self, forum: Any, credentials: dict[str, Any] | None = None) -> None:
        super().__init__(forum)
        self.credentials = credentials or {}

    def _submission_to_raw(self, sub) -> RawPost:
        posted_at = datetime.fromtimestamp(sub.created_utc, tz=timezone.utc)
        body = (sub.selftext or "")[:1500]
        return RawPost(
            external_id=sub.id,
            title=sub.title or "",
            url=f"https://www.reddit.com{sub.permalink}",
            author=str(sub.author) if sub.author else "[deleted]",
            body=body,
            posted_at=posted_at,
            extra={"subreddit": self.cfg.get("subreddit")},
        )

    def fetch_recent(self, since: datetime) -> list[RawPost]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if not self.credentials:
            raise RedditCredentialsMissing(
                "Reddit credentials not configured — add them in Settings → Accounts."
            )

        reddit = _make_reddit(self.credentials)
        sub_name = self.cfg.get("subreddit")
        if not sub_name:
            return []
        subreddit = reddit.subreddit(sub_name)
        since_ts = since.timestamp()
        seen: dict[str, RawPost] = {}

        # 1) Recent submissions.
        limit = int(self.cfg.get("new_limit", DEFAULT_NEW_LIMIT))
        for sub in subreddit.new(limit=limit):
            if sub.created_utc < since_ts:
                break  # .new() is newest-first; older -> stop.
            seen[sub.id] = self._submission_to_raw(sub)

        # 2) Keyword search for noisy subs.
        search_terms = self.cfg.get("search_terms", [])
        for term in search_terms:
            try:
                for sub in subreddit.search(term, sort="new", time_filter=SEARCH_TIME_FILTER, limit=100):
                    if sub.created_utc < since_ts:
                        continue
                    if sub.id not in seen:
                        seen[sub.id] = self._submission_to_raw(sub)
            except Exception:  # noqa: BLE001 - one bad query shouldn't fail the scan
                continue

        return list(seen.values())

    def post_reply(self, post, body: str, credentials: dict) -> PostResult:
        creds = credentials or self.credentials
        try:
            reddit = _make_reddit(creds)
        except RedditCredentialsMissing as exc:
            return PostResult(ok=False, error=str(exc))
        try:
            submission = reddit.submission(id=post.external_id)
            comment = submission.reply(body)
            url = f"https://www.reddit.com{comment.permalink}"
            return PostResult(ok=True, url=url)
        except Exception as exc:  # noqa: BLE001 - surface PRAW errors to the operator
            return PostResult(ok=False, error=f"Reddit posting failed: {exc}")
