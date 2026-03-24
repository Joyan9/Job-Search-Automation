"""
sheets_writer.py — appends scored jobs to Google Sheets via service account.

Auth uses a service account JSON (stored as GOOGLE_CREDENTIALS_JSON env var).
The sheet must be shared with the service account email before first run.

Column layout:
  A: Date Posted  B: Date Added  C: Title  D: Company  E: Location
  F: Score  G: Summary  H: Title Match  I: Tools Match  J: Seniority Fit
  K: Concerns  L: Apply Link  M: Source  N: Status (blank — user fills this)
"""

import logging
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

from src.config import AppConfig
from src.scorer import ScoredJob

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_ROW = [
    "Date Posted",
    "Date Added",
    "Title",
    "Company",
    "Location",
    "Score",
    "Summary",
    "Title Match",
    "Tools Match",
    "Seniority Fit",
    "Concerns",
    "Apply Link",
    "Source",
    "Status",  # User fills: Apply / Skip / Applied / Rejected
]


def _get_worksheet(config: AppConfig) -> gspread.Worksheet:
    creds = Credentials.from_service_account_info(config.google_credentials, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(config.spreadsheet_id)

    try:
        ws = spreadsheet.worksheet(config.sheet_name)
    except gspread.WorksheetNotFound:
        logger.info(f"Sheet '{config.sheet_name}' not found — creating it")
        ws = spreadsheet.add_worksheet(title=config.sheet_name, rows=1000, cols=20)

    return ws


def _ensure_header(ws: gspread.Worksheet) -> None:
    """Write header row if the sheet is empty or header is missing."""
    first_row = ws.row_values(1)
    if first_row != HEADER_ROW:
        ws.insert_row(HEADER_ROW, index=1)
        logger.info("Header row written to sheet")


def _scored_job_to_row(scored: ScoredJob) -> list:
    today = date.today().isoformat()
    return [
        scored.job.date_posted,
        today,
        scored.job.title,
        scored.job.company,
        scored.job.location,
        round(scored.score, 1),
        scored.summary,
        scored.title_match,
        scored.tools_match,
        scored.seniority_fit,
        scored.concerns,
        scored.job.apply_link,
        scored.job.source,
        "",  # Status — blank, user fills in
    ]


def append_jobs(scored_jobs: list[ScoredJob], config: AppConfig) -> int:
    """
    Append all scored jobs to the Google Sheet.
    Returns the number of rows successfully written.
    """
    if not scored_jobs:
        logger.info("No jobs to write to sheet")
        return 0

    ws = _get_worksheet(config)
    _ensure_header(ws)

    rows = [_scored_job_to_row(s) for s in scored_jobs]

    # Batch append for efficiency (single API call)
    ws.append_rows(rows, value_input_option="USER_ENTERED")

    logger.info(f"Wrote {len(rows)} jobs to '{config.sheet_name}' tab")
    return len(rows)
