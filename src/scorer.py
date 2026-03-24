"""
scorer.py — scores job fit using Groq LLM (llama-3.3-70b-versatile).

Sends each job's title + truncated description to the LLM with a structured
prompt. Returns a ScoredJob with numeric score (0-10) and human-readable
reasoning fields. Jobs below config.score_threshold are discarded before
being written to Sheets.
"""

import json
import logging
import time
from dataclasses import dataclass

from groq import Groq

from src.config import AppConfig
from src.fetcher import JobPost

logger = logging.getLogger(__name__)

CANDIDATE_PROFILE = """
Name: Joyan (Data Analyst / Analytics Engineer, Berlin)

Experience: 2+ years total
  - DataVinci Analytics Consultancy (consultant, ~2 yrs)
  - AUTO1 Group (working student)
  - M.Sc. Computer Science, IU Internationale Hochschule Berlin

Core tools & skills:
  SQL, Python, Google Analytics 4 (GA4), Google Tag Manager (GTM),
  Rudderstack, BigQuery, dbt, Looker Studio, Power BI, Tableau,
  Metabase, Excel, Google Sheets

Target roles (in order of fit):
  1. Data Analyst
  2. Analytics Engineer
  3. Web Analyst
  4. Business Analyst (data-focused)
  NOTE: Roles with unusual titles (e.g. "Insights Analyst", "Marketing Data Analyst",
  "Digital Analytics Specialist") should be treated as target roles if the core work
  is data analysis or analytics engineering.

Location: Open to all work setups (remote/hybrid/onsite) across Germany.

Seniority preference:
  - ≤ 2 years required experience → perfect fit (no penalty)
  - 3+ years required → still worth applying, but score lower
  - Senior/Lead/Head titles → score ≤ 5 unless JD is clearly entry-friendly
"""

SCORING_PROMPT_TEMPLATE = """
You are a strict but fair recruiter evaluating job-candidate fit.

CANDIDATE PROFILE:
{profile}

JOB LISTING:
Title: {title}
Company: {company}
Location: {location}
Employment Type: {employment_type}

Description (truncated to {max_chars} chars):
{description}

---

Score this job's fit for the candidate on a scale of 0.0 to 10.0.

Scoring rubric:
- 9-10: Excellent fit. Title matches, tools align well, seniority is a match.
- 7-8:  Good fit. Minor gaps but candidate should apply confidently.
- 5-6:  Moderate fit. Apply with a strong cover letter. Notable gaps exist.
- 3-4:  Weak fit. Significant skill or seniority mismatch.
- 0-2:  Not a fit. Wrong domain, heavily mismatched requirements.

Respond ONLY with a valid JSON object in this exact schema. No markdown, no preamble:
{{
  "score": <float 0.0-10.0>,
  "title_match": "<one sentence: is the role title a match?>",
  "tools_match": "<one sentence: which required tools does candidate have / lack?>",
  "seniority_fit": "<one sentence: does years of experience match?>",
  "location_ok": <true/false>,
  "concerns": "<one sentence: biggest gap or red flag, or 'None'>",
  "summary": "<two sentences max: overall verdict and apply recommendation>"
}}
"""


@dataclass
class ScoredJob:
    job: JobPost
    score: float
    title_match: str
    tools_match: str
    seniority_fit: str
    location_ok: bool
    concerns: str
    summary: str


def _build_prompt(job: JobPost, config: AppConfig) -> str:
    description = job.description[: config.max_description_chars]
    return SCORING_PROMPT_TEMPLATE.format(
        profile=CANDIDATE_PROFILE,
        title=job.title,
        company=job.company,
        location=job.location,
        employment_type=job.employment_type,
        description=description,
        max_chars=config.max_description_chars,
    )


def _parse_response(raw_text: str, job: JobPost) -> ScoredJob | None:
    """Parse LLM JSON response into ScoredJob. Returns None on parse failure."""
    try:
        # Strip any accidental markdown fences
        cleaned = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(cleaned)
        return ScoredJob(
            job=job,
            score=float(data["score"]),
            title_match=data.get("title_match", ""),
            tools_match=data.get("tools_match", ""),
            seniority_fit=data.get("seniority_fit", ""),
            location_ok=bool(data.get("location_ok", True)),
            concerns=data.get("concerns", ""),
            summary=data.get("summary", ""),
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Failed to parse LLM response for '{job.title}': {e}")
        logger.debug(f"Raw response: {raw_text[:300]}")
        return None


def score_jobs(jobs: list[JobPost], config: AppConfig) -> list[ScoredJob]:
    """
    Score each job via Groq. Returns only jobs that meet score_threshold.
    Jobs that fail to parse get skipped with a warning.
    """
    client = Groq(api_key=config.groq_api_key)
    passed: list[ScoredJob] = []
    total = len(jobs)

    for i, job in enumerate(jobs):
        logger.info(f"[{i+1}/{total}] Scoring: '{job.title}' at {job.company}")

        prompt = _build_prompt(job, config)
        try:
            response = client.chat.completions.create(
                model=config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=config.groq_temperature,
                max_tokens=400,
            )
            raw_text = response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Groq API error for '{job.title}': {e}")
            time.sleep(2)
            continue

        scored = _parse_response(raw_text, job)
        if scored is None:
            continue

        logger.info(f"  → Score: {scored.score:.1f} | {scored.summary[:80]}")

        if scored.score >= config.score_threshold:
            passed.append(scored)
            logger.info(f"  ✓ Passed threshold ({config.score_threshold})")
        else:
            logger.info(f"  ✗ Below threshold — discarded")

        # Brief pause to respect Groq rate limits (free tier: 30 req/min)
        time.sleep(0.5)

    logger.info(f"Scoring complete — {len(passed)}/{total} jobs passed threshold")
    return passed
