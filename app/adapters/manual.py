"""Manual channel adapter (LinkedIn groups, etc.).

LinkedIn prohibits scraping and automated posting. This adapter therefore does
NOT fetch and does NOT post programmatically. These forums surface on the
dashboard as manual channels with a link out and the assisted copy-to-clipboard
draft workflow only. See PROJECT_BRIEF compliance guardrails.
"""

from __future__ import annotations

from datetime import datetime

from .base import Adapter, PostResult, RawPost


class ManualAdapter(Adapter):
    adapter_type = "manual"

    def fetch_recent(self, since: datetime) -> list[RawPost]:
        # No automated fetching for manual channels (compliance).
        return []

    def post_reply(self, post, body: str, credentials: dict) -> PostResult:
        return PostResult(
            ok=False,
            error="Manual channel — LinkedIn automation is prohibited. Use the assisted copy workflow.",
        )
