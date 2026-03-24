"""
main.py — pipeline orchestrator.

Execution order:
  1. Load config
  2. Fetch jobs from JSearch (RapidAPI)
  3. Deduplicate against seen_ids.json
  4. Score new jobs via Groq LLM
  5. Append jobs that pass threshold to Google Sheets
  6. Persist updated seen IDs to disk

Run locally:
  python main.py

Run in CI:
  Triggered by GitHub Actions cron (see .github/workflows/daily_fetch.yml)
"""

import logging
import sys
from datetime import datetime

from src.config import load_config
from src.deduplicator import filter_new_jobs, load_seen_ids, save_seen_ids
from src.fetcher import fetch_jobs
from src.scorer import score_jobs
from src.sheets_writer import append_jobs


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    config = load_config()
    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    run_start = datetime.now()
    logger.info("=" * 60)
    logger.info(f"Job pipeline started — {run_start.strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"Score threshold: {config.score_threshold} | Queries: {len(config.searches)}")
    logger.info("=" * 60)

    # ── Stage 1: Fetch ─────────────────────────────────────────────────────────
    fetched = fetch_jobs(config)
    if not fetched:
        logger.warning("No jobs fetched — exiting early")
        return

    # ── Stage 2: Deduplicate ───────────────────────────────────────────────────
    seen_ids = load_seen_ids(config.seen_ids_path)
    new_jobs = filter_new_jobs(fetched, seen_ids)
    if not new_jobs:
        logger.info("All fetched jobs already seen — nothing to score")
        return

    # ── Stage 3: Score ─────────────────────────────────────────────────────────
    scored_jobs = score_jobs(new_jobs, config)

    # ── Stage 4: Write to Sheets ───────────────────────────────────────────────
    written = append_jobs(scored_jobs, config)

    # ── Stage 5: Persist seen IDs ──────────────────────────────────────────────
    # Mark ALL fetched jobs as seen, not just the ones that passed.
    # This prevents re-scoring rejected jobs on the next run.
    new_seen = seen_ids | {j.job_id for j in fetched}
    save_seen_ids(config.seen_ids_path, new_seen)

    # ── Summary ────────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - run_start).total_seconds()
    logger.info("=" * 60)
    logger.info(f"Run complete in {elapsed:.1f}s")
    logger.info(f"  Fetched:   {len(fetched)}")
    logger.info(f"  New:       {len(new_jobs)}")
    logger.info(f"  Scored:    {len(new_jobs)}")
    logger.info(f"  Passed:    {len(scored_jobs)}")
    logger.info(f"  Written:   {written}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
