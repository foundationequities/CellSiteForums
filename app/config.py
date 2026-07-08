"""Application configuration, filesystem paths, and config-file loading.

Config JSON (``forums.json`` / ``keywords.json``) lives in the repository root
for this project (not a ``config/`` subfolder). We still fall back to a
``config/`` directory so either layout works.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# app/ -> repo root
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "forumagent.db"
LOG_PATH = DATA_DIR / "forumagent.log"

load_dotenv(ROOT_DIR / ".env")

# Politeness / identity — used by every HTTP adapter.
USER_AGENT = "ForumAgent/1.0 (CellSite Solutions; contact@cellsitesolutions.com)"
MIN_REQUEST_INTERVAL_SECONDS = 5.0  # per host
REQUEST_TIMEOUT_SECONDS = 30.0

# Defaults; runtime-adjustable settings are persisted in the DB Setting table.
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_SCAN_INTERVAL_HOURS = 6
DEFAULT_RECENCY_HALF_LIFE_DAYS = 21

# Score thresholds (tunable; persisted as settings).
DEFAULT_THRESHOLD_HIGH = 12.0
DEFAULT_THRESHOLD_MEDIUM = 6.0

HOST = os.getenv("FORUMAGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("FORUMAGENT_PORT", "8420"))


def _find_config_file(name: str) -> Path:
    """Return the path to a config JSON file, root-first then ``config/``."""
    root_candidate = ROOT_DIR / name
    if root_candidate.exists():
        return root_candidate
    config_candidate = ROOT_DIR / "config" / name
    if config_candidate.exists():
        return config_candidate
    # Return the root path anyway so callers get a clear FileNotFoundError.
    return root_candidate


def load_forums() -> list[dict[str, Any]]:
    """Load the forum seed list. Never invent forums — this is the only source."""
    path = _find_config_file("forums.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    forums = data.get("forums", [])
    if not forums:
        raise ValueError(f"{path} contains no 'forums' — refusing to seed an empty list.")
    return forums


def load_keywords() -> dict[str, Any]:
    """Load the keyword taxonomy + buying-signal boosters."""
    path = _find_config_file("keywords.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("keywords"):
        raise ValueError(f"{path} contains no 'keywords'.")
    return data


# Fallback competitor list if competitors.json is missing (still user-editable in Settings).
DEFAULT_COMPETITORS = ["Fibrebond", "Thermobond", "VFP Inc", "Sabre"]


def load_competitors() -> list[str]:
    """Load competitor names to track. Root-first, then config/, then defaults."""
    path = _find_config_file("competitors.json")
    if not path.exists():
        return list(DEFAULT_COMPETITORS)
    data = json.loads(path.read_text(encoding="utf-8"))
    names = data.get("competitors", [])
    return names or list(DEFAULT_COMPETITORS)


# Topic categories tracked as dashboard tabs (post-topic based, from keyword
# categories). TRADES stays a valid topic/filter but is not shown as a tab.
CATEGORIES = ["TOWER", "FIBER", "DATA", "E911", "TRADES"]
TAB_CATEGORIES = ["TOWER", "FIBER", "DATA", "E911"]

# Geography detection (USA focus). Best-effort keyword heuristic used when the
# AI classifier is unavailable, and to pre-tag every post regardless.
US_GEO_SIGNALS = [
    "USA", "U.S.", "United States", "FCC", "BEAD", "RDOF", "NTIA", "FirstNet",
    "USDA ReConnect", "state DOT", "county", "co-op", "cooperative", "municipal",
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
NON_US_GEO_SIGNALS = [
    "United Kingdom", "Britain", "England", "Scotland", "Wales", "Ireland",
    "Ofcom", "Openreach", "Canada", "Canadian", "Ontario", "Quebec",
    "Australia", "Australian", "NBN", "New Zealand", "India", "Germany",
    "France", "Spain", "Italy", "Netherlands", "Nigeria", "Kenya", "Ghana",
    "Philippines", "Malaysia", "Singapore", "Brazil", "Mexico", "province",
    "EU ", "European Union", "£", "€",
]

DEFAULT_AI_CONTEXT = (
    "CellSite Solutions sells physical structures for telecom and broadband "
    "infrastructure: fiber huts, telecom/equipment shelters, and modular "
    "(smaller-building) data centers, plus related enclosures. FOCUS on U.S. "
    "opportunities only — treat non-U.S. discussions as low priority.\n\n"
    "Prioritize, in order:\n"
    "1. DIRECT demand: someone looking to buy/spec/source a fiber hut, telecom "
    "or equipment shelter, or a modular/prefab data center building.\n"
    "2. RELATED / latent demand: a utility, electric co-op, ISP, or carrier "
    "expanding its fiber footprint, FTTH/middle-mile/BEAD build, small-cell or "
    "tower program, or E911/NG911 dispatch expansion — anything that will "
    "likely require a hut, shelter, or modular building even if not stated.\n"
    "3. Not relevant: consumer support, unrelated IT, non-infrastructure chatter.\n\n"
    "Reward posts that imply an upcoming need for a structure, not just exact "
    "keyword matches."
)


def load_geo_signals() -> tuple[list[str], list[str]]:
    """Return (us_signals, non_us_signals), overridable via geo.json if present."""
    path = _find_config_file("geo.json")
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            data.get("us", US_GEO_SIGNALS) or US_GEO_SIGNALS,
            data.get("non_us", NON_US_GEO_SIGNALS) or NON_US_GEO_SIGNALS,
        )
    return list(US_GEO_SIGNALS), list(NON_US_GEO_SIGNALS)


@dataclass
class RuntimeSettings:
    """Effective settings, seeded from defaults and overridable in the DB."""

    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    scan_interval_hours: int = DEFAULT_SCAN_INTERVAL_HOURS
    recency_half_life_days: int = DEFAULT_RECENCY_HALF_LIFE_DAYS
    threshold_high: float = DEFAULT_THRESHOLD_HIGH
    threshold_medium: float = DEFAULT_THRESHOLD_MEDIUM
    extra: dict[str, Any] = field(default_factory=dict)


def has_anthropic_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def anthropic_key() -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    return key or None


def master_key() -> str | None:
    key = os.getenv("FORUMAGENT_KEY", "").strip()
    return key or None


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
