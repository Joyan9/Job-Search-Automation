"""
fetcher.py — pulls job listings from JSearch (RapidAPI).

Each search query = 1 API request. All queries defined in config/queries.yaml.
Results are normalised into a consistent JobPost dataclass before being
passed downstream — nothing else in the pipeline knows about the raw API shape.

Description extraction strategy:
  Rather than blindly taking the first N chars (which is usually company intro),
  we extract two sections and concatenate them:
    1. Opening context (~400 chars) — role summary and company overview
    2. Requirements section (~1500 chars) — detected by header keyword, or the
       last 1500 chars as fallback (requirements almost always appear at the end)
"""

import logging
import json
import re
from pathlib import Path
import time
from dataclasses import dataclass

import requests

from src.config import AppConfig

logger = logging.getLogger(__name__)

@dataclass
class JobPost:
    job_id: str
    title: str
    company: str
    location: str
    employment_type: str
    description: str
    apply_link: str
    date_posted: str
    source: str


JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
REQUEST_DELAY_SECONDS = 4.0  # polite gap between requests — JSearch throttles fast bursts

# Requirements section headers — English and German
# Sorted longest-first so more specific phrases match before shorter ones
REQUIREMENTS_HEADERS = sorted([
    # English
    "What we're looking for",
    "What you'll bring",
    "What you will bring",
    "What you bring",
    "What you need",
    "The ideal candidate",
    "Skills & experience",
    "Skills and experience",
    "Who you are",
    "Your profile",
    "Your background",
    "Your experience",
    "About you",
    "Must have",
    "You will have",
    "You should have",
    "You have",
    "You bring",
    "Requirements",
    "Qualifications",
    # German — formal Sie (common in corporate JDs)
    "Was Sie mitbringen sollten",
    "Was Sie mitbringen",
    "Das bringen Sie mit",
    "Ihre Qualifikationen",
    "Ihr Profil",
    "Wofür wir Sie suchen",
    "Was wir uns wünschen",
    "Das wünschen wir uns",
    "Wir wünschen uns",
    "Anforderungen",
    "Voraussetzungen",
    "Wir suchen",
    # German — informal du (common in startups/tech)
    "Was du mitbringen solltest",
    "Was du mitbringst",
    "Das bringst du mit",
    "Deine Qualifikationen",
    "Dein Profil",
    "Deine Stärken",
    "Über dich",
    # German — section headers seen in JSearch-scraped JDs (often all-caps on site, mixed here)
    "Was Sie mitbringen sollten",
    "Ihre Aufgaben",       # tasks section — requirements usually follow immediately
    "Das bringen Sie mit",
    "Überzeugend – Deine Kenntnisse & Erfahrungen",
    "Deine Zutaten:",
    "Was uns überzeugt",
    "Education & Experience:",
    "DEINKNOWHOW",
    "Du …",
    "Das solltest du an Qualifikationen mitbringen"
], key=len, reverse=True)

# Headers that signal the END of requirements (benefits/offer section).
# We stop extracting when we hit one of these.
END_HEADERS = [
    "Was wir bieten", "Was wir Ihnen bieten", "Das bieten wir",
    "Das bieten wir dir", "Das bieten wir Ihnen",
    "Wir bieten", "Unser Angebot", "Benefits", "Perks",
    "What we offer", "What we can offer", "We offer",
    "Our benefits", "Why us", "Why join us",
]


# Hard cap sent to the LLM — keeps token usage predictable.
LLM_CHAR_LIMIT = 3000

# Where to log JDs that had no requirements header match.
# Review this file daily and add new headers to REQUIREMENTS_HEADERS.
UNMATCHED_LOG_PATH = Path(__file__).parent.parent / "data" / "unmatched_jds.jsonl"


def _log_unmatched_jd(full_text: str) -> None:
    """Append the raw JD to a JSONL file for manual header review."""
    UNMATCHED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(UNMATCHED_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"jd": full_text}, ensure_ascii=False) + "\n")



def _extract_description(full_text: str, opening_chars: int = 400) -> str:
    """
    Extract the most scoring-relevant parts of a job description.

    Strategy:
      1. Search the ENTIRE raw JD for a requirements section header.
      2. Take everything from that header to the next end-section header
         (benefits, offer etc.) with no character cap on the extraction itself.
      3. Combine opening context + requirements, then trim to LLM_CHAR_LIMIT.

    This ensures requirements are always captured regardless of JD length.
    """
    if not full_text:
        return ""

    opening = full_text[:opening_chars].strip()

    # Step 1: find earliest requirements header in the full text
    req_start = None
    matched_header = None
    for header in REQUIREMENTS_HEADERS:
        pattern = re.compile(re.escape(header), re.IGNORECASE)
        match = pattern.search(full_text)
        if match:
            if req_start is None or match.start() < req_start:
                req_start = match.start()
                matched_header = header

    if req_start is not None:
        # Step 2: find end of requirements section in full remaining text
        text_from_req = full_text[req_start:]
        req_end = len(text_from_req)
        for end_header in END_HEADERS:
            end_pattern = re.compile(re.escape(end_header), re.IGNORECASE)
            end_match = end_pattern.search(text_from_req)
            if end_match and end_match.start() < req_end:
                req_end = end_match.start()

        requirements = text_from_req[:req_end].strip()
        logger.info(f"  Requirements header matched: '{matched_header}' ({len(requirements)} chars extracted)")
    else:
        # Fallback: take the last portion of the JD
        fallback_start = max(0, len(full_text) - (LLM_CHAR_LIMIT - opening_chars))
        requirements = full_text[fallback_start:].strip()
        logger.info(f"  No requirements header found — fallback ({len(requirements)} chars)")
        # Log full JD for manual review — owner checks daily and adds missing headers
        _log_unmatched_jd(full_text)

    # Step 3: combine
    if req_start is not None and req_start < opening_chars:
        combined = requirements
    else:
        combined = f"{opening}\n\n--- Requirements ---\n{requirements}"

    # Step 4: trim to LLM budget
    if len(combined) > LLM_CHAR_LIMIT:
        combined = combined[:LLM_CHAR_LIMIT]
        logger.info(f"  Trimmed to {LLM_CHAR_LIMIT} chars for LLM")

    return combined


def _parse_job(raw: dict) -> JobPost:
    """Map raw JSearch response keys → JobPost fields."""
    full_description = raw.get("job_description") or ""
    return JobPost(
        job_id=raw.get("job_id", ""),
        title=raw.get("job_title", ""),
        company=raw.get("employer_name", ""),
        location=_build_location(raw),
        employment_type=raw.get("job_employment_type", ""),
        description=_extract_description(full_description),
        apply_link=raw.get("job_apply_link") or raw.get("job_google_link", ""),
        date_posted=(raw.get("job_posted_at_datetime_utc") or "")[:10],
        source=raw.get("job_publisher", ""),
    )



def _build_location(raw: dict) -> str:
    parts = [
        raw.get("job_city", ""),
        raw.get("job_state", ""),
        raw.get("job_country", ""),
    ]
    return ", ".join(p for p in parts if p)


def fetch_jobs(config: AppConfig) -> list[JobPost]:
    """
    Run all configured search queries and return deduplicated JobPost list.
    Uses a job_id set to drop duplicates across queries within the same run.
    """
    headers = {
        "X-RapidAPI-Key": config.rapidapi_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }

    all_jobs: list[JobPost] = []
    seen_in_run: set[str] = set()

    for i, search in enumerate(config.searches):
        query = search.get("query", "")
        location = search.get("location", "")

        full_query = f"{query} in {location}".strip() if location else query

        params = {
            "query": full_query,
            "num_pages": "1",
            "page": "1",
            "country": "de",
            # date_posted omitted: unreliable on non-US indexes, dedup handles reruns
        }

        logger.info(f"[{i+1}/{len(config.searches)}] Fetching: '{full_query}'")

        try:
            resp = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Request failed for query '{query}': {e}")
            continue

        if data.get("status") != "OK":
            logger.warning(f"  API non-OK: {data.get('status')} — {data.get('message', '')}")

        raw_jobs = data.get("data", [])
        logger.info(f"  Status: OK | Jobs: {len(raw_jobs)}")

        for raw in raw_jobs:
            job = _parse_job(raw)
            if not job.job_id or job.job_id in seen_in_run:
                continue
            title_company_key = f"{job.title.lower().strip()}|{job.company.lower().strip()}"
            if title_company_key in seen_in_run:
                logger.debug(f"  Skipping duplicate: {job.title} @ {job.company}")
                continue
            seen_in_run.add(job.job_id)
            seen_in_run.add(title_company_key)
            all_jobs.append(job)

        if i < len(config.searches) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(f"Fetch complete — {len(all_jobs)} unique jobs across all queries")
    return all_jobs