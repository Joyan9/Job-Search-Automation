import logging
import time
from datetime import date
from typing import List

import gspread
from google.oauth2.service_account import Credentials

from src.config import AppConfig
from src.scorer import ScoredJob

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Updated headers to match the new ScoredJob attributes
JOBS_HEADER = [
    "Date Added", "Title", "Company", "Location",
    "Score", "Base Score", "German Req", "Penalty", 
    "Tools Found", "Concerns", "Summary/Reasoning", "Apply Link", "Source",
    "Status"
]

REJECTED_HEADER = [
    "Date Added", "Title", "Company", "Score", "German Req", 
    "Concerns", "Summary/Reasoning", "Apply Link"
]

MAX_RETRIES = 3
RETRY_DELAY = 5 

def _get_spreadsheet(config: AppConfig) -> gspread.Spreadsheet:
    creds = Credentials.from_service_account_info(config.google_credentials, scopes=SCOPES)
    client = gspread.authorize(creds)
    # Using open_by_key as it is more reliable than open by name
    return client.open_by_key(config.spreadsheet_id)

def _get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, name: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        logger.info(f"Sheet '{name}' not found — creating it")
        return spreadsheet.add_worksheet(title=name, rows=100, cols=15)

def _ensure_header(ws: gspread.Worksheet, header: List[str]) -> None:
    first_row = ws.row_values(1)
    if not first_row or first_row[0] != header[0]:
        ws.insert_row(header, index=1)
        logger.info(f"Header written to '{ws.title}'")

def _to_jobs_row(s: ScoredJob) -> List:
    """Maps the new ScoredJob attributes to the 'Jobs' sheet columns."""
    today = date.today().isoformat()
    return [
        today, 
        s.job.title, 
        s.job.company, 
        s.job.location,
        s.score, 
        s.base_score, 
        s.german_required, 
        s.german_penalty,
        ", ".join(getattr(s, 'tools_found', [])), # Join list into string
        s.concerns, 
        s.summary, 
        s.job.apply_link, 
        s.job.source,
        "New" # Default Status
    ]

def _to_rejected_row(s: ScoredJob) -> List:
    """Maps the new ScoredJob attributes to the 'Rejected' sheet columns."""
    today = date.today().isoformat()
    return [
        today, 
        s.job.title, 
        s.job.company, 
        s.score, 
        s.german_required, 
        s.concerns, 
        s.summary, 
        s.job.apply_link
    ]

def _write_rows(ws: gspread.Worksheet, rows: List[List]) -> None:
    """
    Writes rows after the last filled row, expanding the sheet grid if necessary.
    """
    if not rows:
        return

    # 1. Find the last row with data by checking column A
    all_values = ws.col_values(1)  # Get all values in first column
    
    # Find the last non-empty cell by iterating backward
    next_row = 1
    for i in range(len(all_values) - 1, -1, -1):
        if all_values[i].strip():  # If cell is not empty
            next_row = i + 2  # +1 for 0-indexing, +1 for next row
            break
    
    num_new_rows = len(rows)
    required_rows = next_row + num_new_rows - 1

    # 2. Check grid limits and expand if necessary
    current_max_rows = ws.row_count
    if required_rows > current_max_rows:
        # Add the exact amount needed + a 100-row buffer to reduce API calls in future runs
        rows_to_add = (required_rows - current_max_rows) + 100
        ws.add_rows(rows_to_add)
        logger.info(f"Expanded '{ws.title}' by {rows_to_add} rows to accommodate data.")

    # 3. Perform the update
    # Note: Modern gspread expects 'range_name' as first arg, 'values' as second
    ws.update(
        range_name=f"A{next_row}", 
        values=rows, 
        value_input_option="USER_ENTERED"
    )

def append_jobs(passed: List[ScoredJob], rejected: List[ScoredJob], config: AppConfig) -> tuple[int, int]:
    spreadsheet = _get_spreadsheet(config)

    passed_written = 0
    if passed:
        ws = _get_or_create_worksheet(spreadsheet, config.sheet_name)
        _ensure_header(ws, JOBS_HEADER)
        # Ensure we catch any transient errors during the write process
        try:
            _write_rows(ws, [_to_jobs_row(s) for s in passed])
            passed_written = len(passed)
            logger.info(f"Successfully wrote {passed_written} jobs to '{ws.title}'")
        except Exception as e:
            logger.error(f"Failed to write passed jobs: {e}")

    rejected_written = 0
    if rejected:
        ws = _get_or_create_worksheet(spreadsheet, "Rejected")
        _ensure_header(ws, REJECTED_HEADER)
        try:
            _write_rows(ws, [_to_rejected_row(s) for s in rejected])
            rejected_written = len(rejected)
            logger.info(f"Successfully wrote {rejected_written} jobs to 'Rejected'")
        except Exception as e:
            logger.error(f"Failed to write rejected jobs: {e}")

    return passed_written, rejected_written