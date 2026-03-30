# Job Intelligence Pipeline

An automated daily job scraping and AI scoring pipeline built to surface the most relevant data analyst and analytics engineering roles across Germany — and eliminate the noise.

---

## What it does

Every weekday morning, the pipeline:

1. **Fetches** fresh job listings from LinkedIn, Indeed, Glassdoor, and more via the JSearch API (aggregated, no scraping)
2. **Deduplicates** against previously seen jobs so each listing is only scored once
3. **Scores** each job on a 0–10 scale using an LLM (Groq / llama-3.3-70b-versatile) against a detailed candidate profile — title fit, tool overlap, seniority match
4. **Appends** only jobs above the threshold to a shared Google Sheet for morning review

The result: open your sheet each morning and see only pre-filtered, pre-reasoned opportunities.

---

## Architecture

```
GitHub Actions (cron 06:30 CET)
        │
        ▼
┌─────────────────────┐
│   Job Fetcher       │  JSearch (RapidAPI) — 5 queries/day, 10 results each
│   fetcher.py        │  LinkedIn · Indeed · Glassdoor · StepStone · more
└────────┬────────────┘
         │ up to 50 raw jobs/day
         ▼
┌─────────────────────┐
│   Deduplicator      │  Skips jobs already in seen_ids.json
│   deduplicator.py   │  State committed back to repo after each run
└────────┬────────────┘
         │ new jobs only
         ▼
┌─────────────────────┐
│   LLM Scorer        │  Groq (llama-3.3-70b-versatile)
│   scorer.py         │  Returns score 0–10 + structured reasoning JSON
└────────┬────────────┘
         │ score ≥ threshold
         ▼
┌─────────────────────┐
│   Sheets Writer     │  Appends to Google Sheets via service account
│   sheets_writer.py  │  14 columns: score, reasoning, link, status
└────────┬────────────┘
         │
         ▼
    Morning review
    (Apply / Skip / Archive)
```

## Script roles and workflow

1. `src/config.py`
   - Loads `config/queries.yaml`, environment variables from `.env`, and assembles an `AppConfig` dataclass.
   - Enforces required secrets (`RAPIDAPI_KEY`, `GROQ_API_KEY`, `SPREADSHEET_ID`, `GOOGLE_CREDENTIALS_JSON`).
   - Central single source of truth for settings in the pipeline.

2. `src/fetcher.py`
   - Queries JSearch (RapidAPI) for each search term in `config/queries.yaml`.
   - Normalises responses into `JobPost` objects and extracts high-value JD text for scoring.
   - Implements duplicate filtering within one run (same ID or title/company combo) and respects request delay.

3. `src/deduplicator.py`
   - Loads existing job IDs from `data/seen_ids.json`.
   - Filters out previously processed jobs to avoid repeated scoring.
   - Saves updated `seen_ids.json` at the end of each run.

4. `src/scorer.py`
   - Sends each new job to Groq with a structured system/user prompt.
   - Validates response JSON against `ScoredJobResponse` Pydantic model.
   - Applies German proficiency penalty and threshold logic to derive `ScoredJob.score` and pass/fail status.

5. `src/sheets_writer.py`
   - Connects to Google Sheets via service account JSON.
   - Ensures `Jobs` and `Rejected` sheets exist and have headers.
   - Appends passed and rejected jobs as rows (with retry/grid expansion logic).

6. `main.py`
   - Orchestrates the full pipeline: config → fetch → dedupe → score → write → persist.
   - Logs a concise run summary and metrics for visibility.

### Workflow connections

- `main.py` starts by calling `load_config()` from `src/config.py`.
- `fetch_jobs(config)` pulls raw listings and structures them, then returns to `main`.
- `load_seen_ids()` and `filter_new_jobs()` ensure only fresh jobs go to scoring.
- `score_jobs(new_jobs, config)` evaluates each new job and partitions to passed/rejected.
- `append_jobs(passed, rejected, config)` writes outcomes to the spreadsheet.
- Finally, `save_seen_ids()` persists all fetched job IDs (not only passed) to avoid reprocessing.

**Result:** the pipeline always fetches the latest listings, dedups what you already reviewed, scores relevance automatically, and updates Google Sheets for quick daily decisioning.

**Request budget:** 5 queries/day × 22 weekdays = ~110 requests/month (200/month cap on RapidAPI free tier)

---

## Tech stack

| Layer | Tool |
|---|---|
| Orchestration | GitHub Actions (cron) |
| Job data | JSearch via RapidAPI |
| LLM scoring | Groq (llama-3.3-70b-versatile) |
| Output | Google Sheets (gspread + service account) |
| Language | Python 3.11 |
| State persistence | JSON file committed to repo |

---

## Design decisions

- Decision: pass only extracted requirements text to the LLM for scoring. This reduces token usage and focuses scoring on hard skill alignment, minimizing noise from non-essential JD content.
- Decision: use a curated list of common “requirements” section headers (`Requirements`, `Qualifications`, `What you need`, etc.) when extracting requirements from complete JD text. Jobs without a recognized header are logged for manual review so coverage improves over time.
- Decision: do not discard jobs below threshold; write them to a secondary tab (`Rejected`) instead. This enables auditability and ensures the LLM’s filtering behavior is easily validated and tuned.
- Decision: split prompts into system and user roles. The system prompt sets persona/task context (LLM role as job-candidate matcher), while the user prompt provides candidate profile + job requirements + desired return format. This separation improves consistency and result quality.
- Decision: request reasoning before final scores in the prompt. Having the LLM output score components plus reasoning creates a transparent chain-of-thought and strengthens alignment.
- Decision: represent LLM output with a validated data class (Pydantic model) for robust structure checks, guard rails, and early error detection.
- Decision: compute the final score in Python from component values (instead of asking LLM to calculate). This saves tokens and eliminates arithmetic inconsistencies from the model.

---

## Repository structure

```
job-pipeline/
├── .github/
│   └── workflows/
│       └── daily_fetch.yml     # Cron schedule, secret injection, commit-back
├── config/
│   └── queries.yaml            # All search queries and settings — edit this
├── data/
│   └── seen_ids.json           # Auto-updated: tracks processed job IDs
├── src/
│   ├── config.py               # Env loader, settings dataclass
│   ├── fetcher.py              # JSearch API wrapper → JobPost dataclass
│   ├── deduplicator.py         # Load/save/filter seen IDs
│   ├── scorer.py               # Groq prompt, JSON parsing → ScoredJob
│   └── sheets_writer.py        # Auth, header setup, batch append
├── tests/                      # Unit tests (see Testing section)
├── main.py                     # Orchestrator — wires all stages together
├── requirements.txt
└── .env.example
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/job-pipeline.git
cd job-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Get API keys

| Service | Where to get it |
|---|---|
| RapidAPI (JSearch) | [rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) |
| Groq | [console.groq.com/keys](https://console.groq.com/keys) |

### 3. Google Sheets service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → Create project
2. Enable **Google Sheets API** and **Google Drive API**
3. Create a **Service Account** → generate JSON key
4. Copy the entire JSON key content → paste into `GOOGLE_CREDENTIALS_JSON` in `.env`
5. Create a Google Sheet → copy the ID from the URL
6. Share the sheet with the service account email (`...@...iam.gserviceaccount.com`) — Editor access

### 4. Fill in `.env`

```env
RAPIDAPI_KEY=your_key
GROQ_API_KEY=your_key
GOOGLE_CREDENTIALS_JSON={"type":"service_account",...}
SPREADSHEET_ID=your_sheet_id
```

### 5. Run locally

```bash
python main.py
```

### 6. Deploy to GitHub Actions

Add these **secrets** in your repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `RAPIDAPI_KEY` | Your RapidAPI key |
| `GROQ_API_KEY` | Your Groq key |
| `GOOGLE_CREDENTIALS_JSON` | Full service account JSON (paste as-is) |
| `SPREADSHEET_ID` | Your Google Sheet ID |

The pipeline runs automatically every weekday at 06:30 CET.

---

## Configuration

Edit `config/queries.yaml` to adjust search queries or scoring settings:

```yaml
searches:
  - query: "data analyst"
    location: "Berlin, Germany"
    date_posted: "today"

settings:
  score_threshold: 6.0          # Raise to 7.0 for fewer, higher-quality results
  groq_model: "llama-3.3-70b-versatile"
  max_description_chars: 2000
```

---

## Google Sheet columns

### Jobs sheet

| Column | Content |
|---|---|
| A | Date Added |
| B | Title |
| C | Company |
| D | Location |
| E | Score |
| F | Base Score |
| G | German Req |
| H | Penalty |
| I | Tools Found |
| J | Concerns |
| K | Summary/Reasoning |
| L | Apply Link |
| M | Source |
| N | Status |

### Rejected sheet

| Column | Content |
|---|---|
| A | Date Added |
| B | Title |
| C | Company |
| D | Score |
| E | German Req |
| F | Concerns |
| G | Summary/Reasoning |
| H | Apply Link |

---

## Design decisions

**Why JSearch instead of scraping?**
LinkedIn and Indeed aggressively block scrapers and change their HTML frequently. JSearch is a maintained aggregator that hits multiple job boards reliably. It counts against a monthly API cap, so queries are designed to maximise signal per request.

**Why commit seen_ids.json to the repo?**
A persistent store without a database. The GitHub Actions workflow commits the updated file back after each run. This means the state survives across runs, is version-controlled, and requires zero infrastructure beyond the repo.

**Why Groq instead of OpenAI?**
Groq's free tier is generous enough to score 50+ jobs/day with no cost, and llama-3.3-70b-versatile produces high-quality structured JSON when prompted correctly. The scorer module is model-agnostic — swap the model string in `queries.yaml` to switch.

---

## Author

Joyan — Data Analyst & Analytics Engineer, Berlin  
[LinkedIn](https://www.linkedin.com/in/joyan-bhathena/)
