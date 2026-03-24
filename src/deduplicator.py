"""
deduplicator.py — tracks job IDs that have already been processed.

State is stored in data/seen_ids.json, which is committed back to the repo
by the GitHub Actions workflow after each successful run. This gives us
persistence without needing a database.
"""

import json
import logging
from pathlib import Path

from src.fetcher import JobPost

logger = logging.getLogger(__name__)


def load_seen_ids(path: Path) -> set[str]:
    """Load previously seen job IDs from disk. Returns empty set if file missing."""
    if not path.exists():
        logger.info("No seen_ids.json found — starting fresh.")
        return set()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ids = set(data.get("job_ids", []))
    logger.info(f"Loaded {len(ids)} previously seen job IDs")
    return ids


def save_seen_ids(path: Path, seen_ids: set[str]) -> None:
    """Persist updated seen IDs back to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"job_ids": sorted(seen_ids)}, f, indent=2)
    logger.info(f"Saved {len(seen_ids)} seen job IDs to {path}")


def filter_new_jobs(jobs: list[JobPost], seen_ids: set[str]) -> list[JobPost]:
    """Return only jobs whose IDs haven't been seen before."""
    new_jobs = [j for j in jobs if j.job_id not in seen_ids]
    skipped = len(jobs) - len(new_jobs)
    logger.info(f"Dedup: {len(new_jobs)} new jobs, {skipped} already seen")
    return new_jobs
