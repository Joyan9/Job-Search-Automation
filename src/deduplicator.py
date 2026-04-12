"""
deduplicator.py — tracks job IDs that have already been processed.

State is stored in data/seen_ids.json, which is committed back to the repo
by the GitHub Actions workflow after each successful run. This gives us
persistence without needing a database.
"""

import json
import logging
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from src.fetcher import JobPost

logger = logging.getLogger(__name__)


@dataclass
class DedupStats:
    new_jobs: int
    seen_id_skipped: int
    stale_skipped: int
    repost_skipped: int


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def build_repost_fingerprint(job: JobPost) -> str:
    """Stable fingerprint to detect recycled listings with new job IDs."""
    normalized = "|".join([
        _normalize(job.title),
        _normalize(job.company),
        _normalize(job.location),
        _normalize(job.description[:600]),
    ])
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _is_stale(date_posted: str, max_job_age_days: int) -> bool:
    if not date_posted:
        return False
    try:
        posted = datetime.strptime(date_posted[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    age_days = (date.today() - posted).days
    return age_days > max_job_age_days


def _is_repost_recent(last_seen_iso: str, repost_cooldown_days: int) -> bool:
    if not last_seen_iso:
        return False
    try:
        last_seen = datetime.strptime(last_seen_iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return (date.today() - last_seen).days <= repost_cooldown_days


def load_seen_state(path: Path) -> tuple[set[str], dict[str, str]]:
    """Load seen IDs and repost fingerprints. Backward compatible with old schema."""
    if not path.exists():
        logger.info("No seen_ids.json found — starting fresh.")
        return set(), {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ids = set(data.get("job_ids", []))
    fingerprints = data.get("fingerprints", {})
    logger.info(f"Loaded {len(ids)} seen IDs and {len(fingerprints)} repost fingerprints")
    return ids, fingerprints


def save_seen_state(path: Path, seen_ids: set[str], fingerprints: dict[str, str]) -> None:
    """Persist updated seen IDs and repost fingerprints back to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "job_ids": sorted(seen_ids),
                "fingerprints": fingerprints,
            },
            f,
            indent=2,
        )
    logger.info(
        "Saved %s seen IDs and %s fingerprints to %s",
        len(seen_ids),
        len(fingerprints),
        path,
    )


def filter_new_jobs(
    jobs: list[JobPost],
    seen_ids: set[str],
    seen_fingerprints: dict[str, str],
    max_job_age_days: int,
    repost_cooldown_days: int,
) -> tuple[list[JobPost], DedupStats]:
    """Return jobs that are unseen, fresh, and not recent reposts."""
    new_jobs: list[JobPost] = []
    seen_id_skipped = 0
    stale_skipped = 0
    repost_skipped = 0

    for job in jobs:
        if job.job_id in seen_ids:
            seen_id_skipped += 1
            continue

        if _is_stale(job.date_posted, max_job_age_days):
            stale_skipped += 1
            continue

        fp = build_repost_fingerprint(job)
        last_seen_iso = seen_fingerprints.get(fp, "")
        if _is_repost_recent(last_seen_iso, repost_cooldown_days):
            repost_skipped += 1
            continue

        new_jobs.append(job)

    stats = DedupStats(
        new_jobs=len(new_jobs),
        seen_id_skipped=seen_id_skipped,
        stale_skipped=stale_skipped,
        repost_skipped=repost_skipped,
    )
    logger.info(
        "Dedup/Freshness: %s new | %s seen-id | %s stale | %s repost",
        stats.new_jobs,
        stats.seen_id_skipped,
        stats.stale_skipped,
        stats.repost_skipped,
    )
    return new_jobs, stats


def update_fingerprints(jobs: list[JobPost], seen_fingerprints: dict[str, str]) -> dict[str, str]:
    """Update fingerprint timestamps for all fetched jobs."""
    today_iso = date.today().isoformat()
    updated = dict(seen_fingerprints)
    for job in jobs:
        updated[build_repost_fingerprint(job)] = today_iso
    return updated


def load_seen_ids(path: Path) -> set[str]:
    """Backward-compatible helper."""
    ids, _ = load_seen_state(path)
    return ids


def save_seen_ids(path: Path, seen_ids: set[str]) -> None:
    """Backward-compatible helper."""
    _, fingerprints = load_seen_state(path)
    save_seen_state(path, seen_ids, fingerprints)
