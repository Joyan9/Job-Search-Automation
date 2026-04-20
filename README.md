# Job Intelligence Pipeline

An automated daily job scraping and AI scoring pipeline built to surface the most relevant data analyst and analytics engineering roles across Germany — and eliminate the noise.

---

## What it does

https://www.loom.com/share/ce7214d173224063a2ae51292c4619c5


Every weekday morning, the pipeline:

1. **Fetches** fresh job listings from LinkedIn, Indeed, Glassdoor, and more via JSearch endpoint(s) (aggregated, no scraping)
2. **Deduplicates + freshness-filters** against previously seen IDs, stale postings, and recent repost fingerprints
3. **Scores** each job on a 0–10 scale using Groq models in consensus mode (average base score + confidence from score spread)
4. **Appends** only jobs above the threshold to a shared Google Sheet for morning review

The result: open your sheet each morning and see only pre-filtered, pre-reasoned opportunities.

---

## Architecture

```
GitHub Actions (cron 06:30 CET)
        │
        ▼
┌─────────────────────┐
│   Job Fetcher       │  JSearch (RapidAPI) endpoint(s) from fetcher.py
│   fetcher.py        │  LinkedIn · Indeed · Glassdoor · StepStone · more
└────────┬────────────┘
         │ raw jobs/day depends on query count and endpoint yield
         ▼
┌─────────────────────┐
│   Deduplicator      │  Seen ID skip + stale age filter + repost cooldown
│   deduplicator.py   │  seen_ids.json stores: job_ids + fingerprints
└────────┬────────────┘
         │ new jobs only
         ▼
┌─────────────────────┐
│   LLM Scorer        │  Groq consensus_models (queries.yaml)
│   scorer.py         │  Returns score + confidence + structured reasoning
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
   - Loads existing state from `data/seen_ids.json` with backward compatibility.
   - Filters out already-seen IDs, stale jobs (`max_job_age_days`), and recent reposts (`repost_cooldown_days`).
   - Persists both ID and repost fingerprint state at the end of each run.

4. `src/scorer.py`
   - Sends each new job to one or more Groq models (`consensus_models`).
   - Validates each response JSON against the `ScoredJobResponse` Pydantic model.
   - Builds a consensus output, computes confidence (`high|medium|low`) from score spread, then applies German penalty and threshold logic.

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
- `load_seen_state()` and `filter_new_jobs()` ensure only unseen, fresh, non-repost jobs go to scoring.
- `score_jobs(new_jobs, config)` evaluates each new job and partitions to passed/rejected.
- `append_jobs(passed, rejected, config)` writes outcomes to the spreadsheet.
- Finally, `save_seen_state()` persists all fetched job IDs plus repost fingerprints (not only passed jobs).

**Result:** the pipeline always fetches the latest listings, dedups what you already reviewed, scores relevance automatically, and updates Google Sheets for quick daily decisioning.

**Request budget (example):** requests/day is approximately `(#searches × #active_endpoints × pages_per_query)`.
With the current default config (4 searches, 1 active endpoint, 1 page), this is about 88 requests/month on weekdays.

---

## Tech stack

| Layer | Tool |
|---|---|
| Orchestration | GitHub Actions (cron) |
| Job data | JSearch via RapidAPI |
| LLM scoring | Groq (consensus-capable, model list in `queries.yaml`) |
| Output | Google Sheets (gspread + service account) |
| Language | Python 3.11 |
| State persistence | JSON file committed to repo |

---

## Design Decisions

To ensure the system remains cost-effective, auditable, and technically robust, the following architectural choices were made:

### Data Sourcing & Persistence
* **3rd Party API over Custom Scraping:** To avoid the constant maintenance required to bypass LinkedIn and Indeed’s anti-scraping measures, we use **JSearch**. This provides reliable, aggregated access to multiple boards. Queries are highly optimized to maximize the "signal" received within API rate limits.
* **Git-Based State Management:** We use a `seen_ids.json` file committed directly to the repository to track processed jobs. This allows **GitHub Actions** to maintain state across runs without the need for external database infrastructure.

### LLM Strategy & Prompt Engineering
* **Cost-Efficient Inference:** We utilize **Groq** to leverage its generous free tier and high-speed inference. The system remains model-agnostic, allowing for easy transitions via `queries.yaml`.
* **Focused Context Window:** Only extracted requirements text is passed to the LLM. By using a curated list of common headers (e.g., *Qualifications*, *What you need*), we minimize token noise and focus scoring strictly on hard-skill alignment.
* **Role Separation:** Prompts are split into **System** (persona and task constraints) and **User** (candidate profile and JD content) roles. This structure improves instruction following and output consistency.
* **Chain-of-Thought Reasoning:** The prompt requires the LLM to output reasoning *before* providing final scores. This forces a transparent "chain-of-thought" that improves the quality of the evaluation.

### Engineering & Validation
* **Calculated vs. Generative Scoring:** To eliminate LLM arithmetic errors and save tokens, the LLM provides raw component values while **Python handles the final score calculation**.
* **Structured Data Integrity:** We use **Pydantic models** to validate LLM outputs. This ensures the data adheres to a strict schema and provides a robust guardrail against hallucinations or malformed JSON.
* **Non-Destructive Filtering:** Jobs falling below the scoring threshold are moved to a **"Rejected" tab** rather than being deleted. This enables manual auditing to fine-tune the LLM’s filtering logic over time.

---

## Repository structure

```
job-pipeline/
├── .github/
│   └── workflows/
│       └── main.yml           # Active workflow: cron, secret injection, commit-back
├── config/
│   └── queries.yaml            # All search queries and settings — edit this
├── data/
│   ├── seen_ids.json           # Auto-updated: tracks processed job IDs + repost fingerprints
│   ├── unmatched_jds.jsonl     # Auto-appended: JDs where requirements header was not detected
│   └── no_result_api_calls.json # Auto-appended: successful API calls that returned 0 jobs
├── src/
│   ├── config.py               # Env loader, settings dataclass
│   ├── fetcher.py              # JSearch API wrapper → JobPost dataclass
│   ├── deduplicator.py         # Load/save/filter seen IDs + repost fingerprints
│   ├── scorer.py               # Groq scoring + consensus + confidence
│   └── sheets_writer.py        # Auth, header setup, batch append
├── tests/                      # Unit tests (see Testing section)
├── main.py                     # Orchestrator — wires all stages together
├── requirements.txt
└── daily_fetch.yml            # Optional/local workflow copy
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/job-pipeline.git
cd job-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file manually in the project root.

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
SHEET_NAME=Jobs
SCORE_THRESHOLD=6.0
LOG_LEVEL=INFO
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
  - query: "analytics engineer"

settings:
  results_per_query: 10
  score_threshold: 6.0          # Raise to 7.0 for fewer, higher-quality results
  groq_model: "meta-llama/llama-4-scout-17b-16e-instruct"
  consensus_models:
    - "meta-llama/llama-4-scout-17b-16e-instruct"
    - "llama-3.3-70b-versatile"
  groq_temperature: 0.1
  max_description_chars: 3000
  max_job_age_days: 21
  repost_cooldown_days: 28
```

## Dedup state format

`data/seen_ids.json` now uses this structure:

```json
{
  "job_ids": ["..."],
  "fingerprints": {
    "<sha1-fingerprint>": "YYYY-MM-DD"
  }
}
```

`job_ids` prevents reprocessing known IDs. `fingerprints` prevents quick reposts with new IDs.

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

## Author

Joyan — [LinkedIn](https://www.linkedin.com/in/joyan-bhathena/)
