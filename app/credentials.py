"""Fernet-encrypted credential store.

Credentials are encrypted at rest with a master key from ``FORUMAGENT_KEY``,
decrypted only in memory at use time, masked in the UI, and never logged or
committed. Each credential's plaintext is a JSON blob whose shape depends on
``kind``:
  reddit_oauth       -> {client_id, client_secret, username, password, user_agent?}
  discourse_api_key  -> {api_key, api_username, base_url?}
  username_password  -> {username, password}
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import Credential, utcnow


class MasterKeyMissing(RuntimeError):
    pass


def generate_key() -> str:
    """Generate a new Fernet master key (base64 string)."""
    return Fernet.generate_key().decode("utf-8")


def _fernet() -> Fernet:
    key = config.master_key()
    if not key:
        raise MasterKeyMissing(
            "FORUMAGENT_KEY is not set. Generate one with "
            "`python -m app.main --generate-key` and add it to your .env."
        )
    return Fernet(key.encode("utf-8"))


def credentials_available() -> bool:
    return bool(config.master_key())


def encrypt_blob(data: dict[str, Any]) -> bytes:
    return _fernet().encrypt(json.dumps(data).encode("utf-8"))


def decrypt_blob(blob: bytes) -> dict[str, Any]:
    try:
        raw = _fernet().decrypt(blob)
    except InvalidToken as exc:
        raise ValueError(
            "Could not decrypt credential — FORUMAGENT_KEY may have changed."
        ) from exc
    return json.loads(raw.decode("utf-8"))


def save_credential(
    db: Session, *, forum_id: int | None, kind: str, data: dict[str, Any]
) -> Credential:
    """Create or replace the credential for a (forum, kind) pair."""
    blob = encrypt_blob(data)
    existing = db.scalar(
        select(Credential).where(
            Credential.forum_id == forum_id, Credential.kind == kind
        )
    )
    if existing:
        existing.encrypted_blob = blob
        existing.verify_status = "unverified"
        existing.last_verified_at = None
        cred = existing
    else:
        cred = Credential(forum_id=forum_id, kind=kind, encrypted_blob=blob)
        db.add(cred)
    db.commit()
    return cred


def get_credential_data(db: Session, cred: Credential) -> dict[str, Any]:
    return decrypt_blob(cred.encrypted_blob)


def get_reddit_credentials(db: Session) -> dict[str, Any] | None:
    """Reddit credentials are global (one script app), stored with forum_id NULL."""
    cred = db.scalar(
        select(Credential).where(Credential.kind == "reddit_oauth")
    )
    if cred is None:
        return None
    try:
        return decrypt_blob(cred.encrypted_blob)
    except ValueError:
        return None


def get_forum_credentials(db: Session, forum_id: int) -> tuple[Credential, dict[str, Any]] | None:
    cred = db.scalar(select(Credential).where(Credential.forum_id == forum_id))
    if cred is None:
        return None
    try:
        return cred, decrypt_blob(cred.encrypted_blob)
    except ValueError:
        return None


def mask(value: str, show: int = 4) -> str:
    """Mask a secret for display: keep the last ``show`` chars."""
    if not value:
        return ""
    if len(value) <= show:
        return "•" * len(value)
    return "•" * (len(value) - show) + value[-show:]


def masked_summary(data: dict[str, Any]) -> dict[str, str]:
    """Return a display-safe, masked view of a credential blob."""
    out: dict[str, str] = {}
    sensitive = {"client_secret", "password", "api_key"}
    for k, v in data.items():
        if not isinstance(v, str):
            v = str(v)
        out[k] = mask(v) if k in sensitive else v
    return out


def mark_verified(db: Session, cred: Credential, status: str) -> None:
    cred.verify_status = status
    cred.last_verified_at = utcnow()
    db.commit()


def verify_reddit(data: dict[str, Any]) -> tuple[bool, str]:
    """Verify Reddit creds by fetching own identity."""
    try:
        from .adapters.reddit import _make_reddit

        reddit = _make_reddit(data)
        me = reddit.user.me()
        if me is None:
            return False, "Authenticated but no user returned."
        return True, f"OK as u/{me.name}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed: {exc}"


def verify_discourse(data: dict[str, Any]) -> tuple[bool, str]:
    """Verify Discourse API key via an authed call."""
    import httpx

    base = (data.get("base_url") or "").rstrip("/")
    if not base:
        return False, "Missing base_url."
    headers = {
        "Api-Key": data.get("api_key", ""),
        "Api-Username": data.get("api_username", ""),
        "User-Agent": config.USER_AGENT,
    }
    try:
        with httpx.Client(timeout=20.0, headers=headers) as client:
            resp = client.get(f"{base}/session/current.json")
    except httpx.HTTPError as exc:
        return False, f"Network error: {exc}"
    if resp.status_code == 200:
        return True, "OK"
    return False, f"HTTP {resp.status_code}"


def _datetime_str(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "never"
