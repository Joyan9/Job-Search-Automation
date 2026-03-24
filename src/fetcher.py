"""
fetcher.py — pulls job listings from JSearch (RapidAPI).

Each search query = 1 API request. All queries defined in config/queries.yaml.
Results are normalised into a consistent JobPost dataclass before being
passed downstream — nothing else in the pipeline knows about the raw API shape.
"""

import logging
import time
from dataclasses import dataclass, field

import requests

from src.config import AppConfig

logger = logging.getLogger(__name__)

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
REQUEST_DELAY_SECONDS = 1.5  # polite gap between requests


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


def _parse_job(raw: dict) -> JobPost:
    """Map raw JSearch response keys → JobPost fields."""
    return JobPost(
        job_id=raw.get("job_id", ""),
        title=raw.get("job_title", ""),
        company=raw.get("employer_name", ""),
        location=_build_location(raw),
        employment_type=raw.get("job_employment_type", ""),
        description=(raw.get("job_description") or "")[:5000],  # store full, scorer truncates
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
        #date_posted = search.get("date_posted", "3days")

        # JSearch expects location embedded in the query string, e.g.
        # "data analyst in Berlin Germany" — not as a separate param
        full_query = f"{query} in {location}".strip() if location else query

        params = {
            "query": full_query,
            "num_pages": "1",
            "page": "1",
            "country": "de"
        }
        logger.debug(f"  Params sent: {params}")

        logger.info(f"[{i+1}/{len(config.searches)}] Fetching: '{full_query}'")

        try:
            resp = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"  Status: {data.get('status')} | Jobs: {len(data.get('data', []))}")
            if data.get('status') != 'OK':
                logger.error(f"  Full response: {data}")
        except requests.RequestException as e:
            logger.error(f"Request failed for query '{query}': {e}")
            continue

        # Surface any API-level error message for easier debugging
        if data.get("status") != "OK":
            logger.warning(f"  API returned non-OK status: {data.get('status')} — {data.get('message', '')}")

        raw_jobs = data.get("data", [])
        logger.info(f"  → {len(raw_jobs)} results returned")

        for raw in raw_jobs:
            job = _parse_job(raw)
            if not job.job_id or job.job_id in seen_in_run:
                continue
            seen_in_run.add(job.job_id)
            all_jobs.append(job)

        # Respect rate limits between requests
        if i < len(config.searches) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(f"Fetch complete — {len(all_jobs)} unique jobs across all queries")
    return all_jobs