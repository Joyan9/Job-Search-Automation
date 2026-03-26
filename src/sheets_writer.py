"""
sheets_writer.py — writes scored jobs to Google Sheets.

Two tabs:
  "Jobs"     — passed jobs (score >= threshold), for morning review
  "Rejected" — below-threshold jobs, for scorer calibration auditing

Append strategy: explicitly finds the last filled row and writes after it,
so pre-allocated empty rows in the sheet don't cause data to be written at
row 1000+ instead of row 2.
"""

import logging
import time
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

JOBS_HEADER = [
    "Date Posted", "Date Added", "Title", "Company", "Location",
    "Score", "Base Score", "Summary", "Title Match", "Tools Match", "Seniority Fit",
    "German Required", "German Penalty", "Concerns", "Apply Link", "Source",
    "Status",  # User fills: Apply / Skip / Applied / Rejected
]

REJECTED_HEADER = [
    "Date Posted", "Date Added", "Title", "Company", "Location",
    "Score", "Seniority Fit", "German Required", "German Penalty",
    "Concerns", "Summary", "Apply Link",
]

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


def _with_retry(fn, *args, **kwargs):
    """Call fn with retries on transient Google API errors (500, 503)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = e.response.status_code if hasattr(e, "response") else 0
            if status in (500, 503) and attempt < MAX_RETRIES:
                logger.warning(f"Google Sheets API error {status} — retry {attempt}/{MAX_RETRIES - 1} in {RETRY_DELAY}s")
                time.sleep(RETRY_DELAY)
            else:
                raise


def _get_spreadsheet(config: AppConfig) -> gspread.Spreadsheet:
    creds = Credentials.from_service_account_info(config.google_credentials, scopes=SCOPES)
    client = gspread.authorize(creds)
    return _with_retry(client.open_by_key, config.spreadsheet_id)


def _get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, name: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        logger.info(f"Sheet '{name}' not found — creating it")
        # Create with minimal rows — sheet grows automatically as rows are added
        return spreadsheet.add_worksheet(title=name, rows=100, cols=20)


def _ensure_header(ws: gspread.Worksheet, header: list[str]) -> None:
    first_row = _with_retry(ws.row_values, 1)
    if first_row != header:
        _with_retry(ws.insert_row, header, index=1)
        logger.info(f"Header written to '{ws.title}'")


def _write_rows(ws: gspread.Worksheet, rows: list[list]) -> None:
    """
    Write rows starting immediately after the last filled row.
    Resizes the sheet first if the new rows would exceed the current grid limit.
    """
    if not rows:
        return

    existing = _with_retry(ws.get_all_values)
    next_row = len(existing) + 1
    required_rows = next_row + len(rows) - 1

    # Expand sheet if needed — add buffer so we dont resize every single run
    if required_rows > ws.row_count:
        new_size = required_rows + 200
        _with_retry(ws.add_rows, new_size - ws.row_count)
        logger.debug(f"  Expanded sheet to {new_size} rows")

    _with_retry(
        ws.update,
        rows,
        f"A{next_row}",
        value_input_option="USER_ENTERED",
    )


def _to_jobs_row(s: ScoredJob) -> list:
    today = date.today().isoformat()
    return [
        s.job.date_posted, today, s.job.title, s.job.company, s.job.location,
        round(s.score, 1), round(s.base_score, 1), s.summary, s.title_match, s.tools_match, s.seniority_fit,
        s.german_required, s.german_penalty, s.concerns, s.job.apply_link, s.job.source,
        "",  # Status — user fills in
    ]


def _to_rejected_row(s: ScoredJob) -> list:
    today = date.today().isoformat()
    return [
        s.job.date_posted, today, s.job.title, s.job.company, s.job.location,
        round(s.score, 1), s.seniority_fit, s.german_required, s.german_penalty,
        s.concerns, s.summary, s.job.apply_link,
    ]


def append_jobs(passed: list[ScoredJob], rejected: list[ScoredJob], config: AppConfig) -> tuple[int, int]:
    """
    Write passed jobs to the Jobs tab, rejected jobs to the Rejected tab.
    Returns (passed_written, rejected_written).
    """
    spreadsheet = _get_spreadsheet(config)

    passed_written = 0
    if passed:
        ws = _get_or_create_worksheet(spreadsheet, config.sheet_name)
        _ensure_header(ws, JOBS_HEADER)
        _write_rows(ws, [_to_jobs_row(s) for s in passed])
        passed_written = len(passed)
        logger.info(f"Wrote {passed_written} jobs to '{config.sheet_name}'")

    rejected_written = 0
    if rejected:
        ws = _get_or_create_worksheet(spreadsheet, "Rejected")
        _ensure_header(ws, REJECTED_HEADER)
        _write_rows(ws, [_to_rejected_row(s) for s in rejected])
        rejected_written = len(rejected)
        logger.info(f"Wrote {rejected_written} jobs to 'Rejected'")

    return passed_written, rejected_written