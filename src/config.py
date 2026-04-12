"""
config.py — centralised settings loader.

All environment variables and YAML config are resolved here.
Nothing else in the codebase reads env vars directly.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "queries.yaml"
SEEN_IDS_PATH = BASE_DIR / "data" / "seen_ids.json"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def load_yaml() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Config dataclass ───────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    # API credentials
    rapidapi_key: str
    groq_api_key: str
    google_credentials: dict

    # Sheets
    spreadsheet_id: str
    sheet_name: str

    # Search settings
    searches: list[dict]
    results_per_query: int
    score_threshold: float
    groq_model: str
    consensus_models: list[str]
    groq_temperature: float
    max_description_chars: int
    max_job_age_days: int
    repost_cooldown_days: int

    # Misc
    log_level: str
    seen_ids_path: Path


def load_config() -> AppConfig:
    raw = load_yaml()
    settings = raw.get("settings", {})

    creds_raw = _require("GOOGLE_CREDENTIALS_JSON")
    try:
        google_credentials = json.loads(creds_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}") from e

    return AppConfig(
        rapidapi_key=_require("RAPIDAPI_KEY"),
        groq_api_key=_require("GROQ_API_KEY"),
        google_credentials=google_credentials,
        spreadsheet_id=_require("SPREADSHEET_ID"),
        sheet_name=_optional("SHEET_NAME", "Jobs"),
        searches=raw.get("searches", []),
        results_per_query=settings.get("results_per_query", 10),
        score_threshold=float(_optional("SCORE_THRESHOLD", str(settings.get("score_threshold", 6.0)))),
        groq_model=settings.get("groq_model", "llama-3.3-70b-versatile"),
        consensus_models=settings.get("consensus_models", [settings.get("groq_model", "llama-3.3-70b-versatile")]),
        groq_temperature=settings.get("groq_temperature", 0.1),
        max_description_chars=settings.get("max_description_chars", 2000),
        max_job_age_days=int(settings.get("max_job_age_days", 21)),
        repost_cooldown_days=int(settings.get("repost_cooldown_days", 28)),
        log_level=_optional("LOG_LEVEL", "INFO"),
        seen_ids_path=SEEN_IDS_PATH,
    )
