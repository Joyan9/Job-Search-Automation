"""
main.py — pipeline orchestrator.

Execution order:
  1. Load config
  2. Fetch jobs from JSearch (RapidAPI)
  3. Deduplicate against seen_ids.json
  4. Score new jobs via Groq LLM → (passed, rejected)
  5. Write passed jobs to "Jobs" tab, rejected to "Rejected" tab
  6. Persist updated seen IDs to disk
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
    passed, rejected = score_jobs(new_jobs, config)

    # ── Stage 4: Write to Sheets ───────────────────────────────────────────────
    passed_written, rejected_written = append_jobs(passed, rejected, config)

    # ── Stage 5: Persist seen IDs ──────────────────────────────────────────────
    new_seen = seen_ids | {j.job_id for j in fetched}
    save_seen_ids(config.seen_ids_path, new_seen)

    # ── Summary ────────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - run_start).total_seconds()
    logger.info("=" * 60)
    logger.info(f"Run complete in {elapsed:.1f}s")
    logger.info(f"  Fetched:   {len(fetched)}")
    logger.info(f"  New:       {len(new_jobs)}")
    logger.info(f"  Passed:    {passed_written}  → 'Jobs' tab")
    logger.info(f"  Rejected:  {rejected_written} → 'Rejected' tab")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()