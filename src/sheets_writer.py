"""
sheets_writer.py — writes scored jobs to Google Sheets.

Two tabs:
  "Jobs"     — passed jobs (score >= threshold), for morning review
  "Rejected" — below-threshold jobs, for scorer calibration auditing
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

JOBS_HEADER = [
    "Date Posted", "Date Added", "Title", "Company", "Location",
    "Score", "Summary", "Title Match", "Tools Match", "Seniority Fit",
    "German Required", "German Penalty", "Concerns", "Apply Link", "Source",
    "Status",  # User fills: Apply / Skip / Applied / Rejected
]

REJECTED_HEADER = [
    "Date Posted", "Date Added", "Title", "Company", "Location",
    "Score", "Seniority Fit", "German Required", "German Penalty",
    "Concerns", "Summary", "Apply Link",
]


def _get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, name: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        logger.info(f"Sheet '{name}' not found — creating it")
        return spreadsheet.add_worksheet(title=name, rows=2000, cols=20)


def _ensure_header(ws: gspread.Worksheet, header: list[str]) -> None:
    if ws.row_values(1) != header:
        ws.insert_row(header, index=1)
        logger.info(f"Header written to '{ws.title}'")


def _get_spreadsheet(config: AppConfig) -> gspread.Spreadsheet:
    creds = Credentials.from_service_account_info(config.google_credentials, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(config.spreadsheet_id)


def _to_jobs_row(s: ScoredJob) -> list:
    today = date.today().isoformat()
    return [
        s.job.date_posted, today, s.job.title, s.job.company, s.job.location,
        round(s.score, 1), s.summary, s.title_match, s.tools_match, s.seniority_fit,
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
        ws.append_rows([_to_jobs_row(s) for s in passed], value_input_option="USER_ENTERED")
        passed_written = len(passed)
        logger.info(f"Wrote {passed_written} jobs to '{config.sheet_name}'")

    rejected_written = 0
    if rejected:
        ws = _get_or_create_worksheet(spreadsheet, "Rejected")
        _ensure_header(ws, REJECTED_HEADER)
        ws.append_rows([_to_rejected_row(s) for s in rejected], value_input_option="USER_ENTERED")
        rejected_written = len(rejected)
        logger.info(f"Wrote {rejected_written} jobs to 'Rejected'")

    return passed_written, rejected_written