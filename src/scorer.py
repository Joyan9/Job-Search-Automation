"""
scorer.py — scores job fit using Groq LLM (llama-3.3-70b-versatile).

Returns two lists: passed (score >= threshold) and rejected (score < threshold).
Both are written to Google Sheets — passed to "Jobs", rejected to "Rejected".
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

German language level: B1 (conversational, not fluent).

Seniority:
  - HARD CAP: Any title containing Senior / Lead / Head / Principal / Staff → score CANNOT exceed 5.0.
    Exception: only exceed 5.0 if the JD explicitly says "junior welcome" or "0-2 years".
  - 3+ years explicitly required → subtract 1.5 from base score (min 0).
  - 1-2 years required or not mentioned → no penalty.
"""

SCORING_PROMPT_TEMPLATE = """
You are a strict recruiter evaluating job-candidate fit. Apply all scoring rules exactly as written.

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

SCORING RULES — apply in order:

1. DOMAIN FILTER (apply first — discard non-data roles immediately):
   - The candidate is a data/analytics professional. Roles must involve working with data.
   - Pure marketing strategy, market research (no data tools), sales, or finance roles → score 3 or below.
   - "Marketing Analyst" is only acceptable if the JD explicitly mentions data tools (SQL, GA4, Python, BI tools).
     If it is a traditional marketing role with no data tools → score ≤ 3.

2. Base score (0-10) based on role and tool fit:
   - 9-10: Title matches perfectly (Data Analyst / Analytics Engineer / Web Analyst), tools align
   - 7-8:  Good fit, role is clearly data-focused, minor gaps
   - 5-6:  Moderate fit, some relevant work but notable gaps
   - 3-4:  Weak fit, tangential to data analytics
   - 0-2:  Wrong domain (pure marketing, sales, finance, HR)

3. Seniority adjustment (apply BEFORE German penalty):
   - Title contains Senior / Lead / Head / Principal / Staff → cap score at 5.0 maximum.
     Only exception: JD explicitly says "junior welcome" or requires ≤ 2 years.
   - JD explicitly requires 3+ years → subtract 1.5 (floor at 0).

4. German language penalty:
   - Candidate level: B1.
   - YOU MUST output german_required as exactly one of these four values: not_required | b1 | b2 | c1_c2
     Do NOT write sentences or explanations in this field. Only one of those four strings.
   - Assessment signals:
     * JD written in German + client-facing/stakeholder communication mentioned → b2
     * JD written in German, no communication requirements → b1 (no penalty)
     * JD written in English → not_required (no penalty)
     * Explicit C1/C2 or "fließend Deutsch" required → c1_c2
   - Penalties:
     * not_required or b1 → german_penalty: 0.0
     * b2 → german_penalty: 1.0
     * c1_c2 → german_penalty: 2.0

5. Final score = base score (after seniority adjustment) minus german_penalty. Minimum 0.

Respond ONLY with a valid JSON object. No markdown, no preamble, no extra text:
{{
  "score": <float 0.0-10.0>,
  "title_match": "<one sentence>",
  "tools_match": "<one sentence>",
  "seniority_fit": "<one sentence — state if Senior/Lead cap was applied>",
  "german_required": "<exactly one of: not_required | b1 | b2 | c1_c2>",
  "german_penalty": <float — 0.0, 1.0, or 2.0 only>,
  "location_ok": <true/false>,
  "concerns": "<one sentence: biggest gap or None>",
  "summary": "<two sentences max: verdict and apply recommendation>"
}}
"""


@dataclass
class ScoredJob:
    job: JobPost
    score: float
    title_match: str
    tools_match: str
    seniority_fit: str
    german_required: str
    german_penalty: float
    location_ok: bool
    concerns: str
    summary: str
    passed: bool  # True if score >= threshold


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
    try:
        cleaned = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(cleaned)
        return ScoredJob(
            job=job,
            score=float(data["score"]),
            title_match=data.get("title_match", ""),
            tools_match=data.get("tools_match", ""),
            seniority_fit=data.get("seniority_fit", ""),
            german_required=data.get("german_required", "not_required"),
            german_penalty=float(data.get("german_penalty", 0.0)),
            location_ok=bool(data.get("location_ok", True)),
            concerns=data.get("concerns", ""),
            summary=data.get("summary", ""),
            passed=False,  # set below
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Failed to parse LLM response for '{job.title}': {e}")
        logger.debug(f"Raw response: {raw_text[:300]}")
        return None


def score_jobs(
    jobs: list[JobPost], config: AppConfig
) -> tuple[list[ScoredJob], list[ScoredJob]]:
    """
    Score all jobs. Returns (passed, rejected) tuple.
    Both lists contain ScoredJob objects — caller decides where to write them.
    """
    client = Groq(api_key=config.groq_api_key)
    passed: list[ScoredJob] = []
    rejected: list[ScoredJob] = []
    total = len(jobs)

    for i, job in enumerate(jobs):
        logger.info(f"[{i+1}/{total}] Scoring: '{job.title}' at {job.company}")

        prompt = _build_prompt(job, config)
        try:
            response = client.chat.completions.create(
                model=config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=config.groq_temperature,
                max_tokens=500,
            )
            raw_text = response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Groq API error for '{job.title}': {e}")
            time.sleep(2)
            continue

        scored = _parse_response(raw_text, job)
        if scored is None:
            continue

        german_note = f" | DE:{scored.german_required} (-{scored.german_penalty})" if scored.german_penalty > 0 else ""
        logger.info(f"  → Score: {scored.score:.1f}{german_note} | {scored.summary[:70]}")

        if scored.score >= config.score_threshold:
            scored.passed = True
            passed.append(scored)
            logger.info(f"  ✓ Passed threshold ({config.score_threshold})")
        else:
            scored.passed = False
            rejected.append(scored)
            logger.info(f"  ✗ Below threshold — rejected")

        time.sleep(2.0)  # 30 TPM budget: 2s gap keeps bursts under 30K TPM

    logger.info(f"Scoring complete — {len(passed)} passed, {len(rejected)} rejected out of {total}")
    return passed, rejected