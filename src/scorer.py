import json
import logging
import time
from typing import Literal, List, Optional
from pydantic import BaseModel, Field, ValidationError
from groq import Groq

from src.config import AppConfig
from src.fetcher import JobPost

logger = logging.getLogger(__name__)

# --- SYSTEM PROMPT: The "Logic & Persona" ---
SYSTEM_PROMPT = """
You are a Lead Technical Recruiter specializing in Data & Analytics. 
Your task is to evaluate Job Descriptions (JDs) against a candidate's profile with 100% objectivity and a "stingy" scoring mindset.

SCORING RUBRIC (Base Score 0-10):
1. Role Alignment (max 4 pts): 4=Perfect match (Data/Analytics Engineer), 2=Tangential, 0=Non-data (Sales/HR).
2. Tool Stack (max 4 pts): 1pt each for: SQL, Python, BI (Looker/PowerBI/Tableau), and Modern Data Stack (dbt/BigQuery/GTM).
3. Experience Fit (max 2 pts): Match for 2 years of experience.

HARD CONSTRAINTS (Apply during reasoning):
- SENIORITY CAP: If Title has "Senior/Lead/Head/Principal" and JD requires >2 years, the base_score CANNOT exceed 5.0.
- DOMAIN FILTER: Pure marketing (no SQL/Python) or pure finance roles = max score 3.0.

GERMAN LANGUAGE CLASSIFICATION:
- 'not_required': JD is English-only.
- 'b1': Basic/Conversational mentioned.
- 'b2': Mixed German/English or "Good German".
- 'c1_c2': Entirely German JD or "Fluency/Verhandlungssicher".
"""

# --- USER PROMPT TEMPLATE: The "Data" ---
USER_PROMPT_TEMPLATE = """
CANDIDATE PROFILE:
- Experience: 2 years (Consultancy & Working Student)
- Tools: SQL, Python, GA4, GTM, dbt, BigQuery, Looker Studio, Power BI
- Target: Data Analyst, Analytics Engineer, Web Analyst
- German: B1 (Conversational)

JOB LISTING:
Title: {title} | Company: {company} | Location: {location}
Description:
{description}

Return ONLY a JSON object:
{{
  "reasoning": "Brief step-by-step logic for score and language choice",
  "base_score": <float 0-10>,
  "german_required": "not_required|b1|b2|c1_c2",
  "seniority_cap_applied": <bool>,
  "tools_found": [<list of matching tools>],
  "concerns": "Biggest gap or None"
}}
"""

class ScoredJobResponse(BaseModel):
    """Schema for LLM response validation."""
    reasoning: str
    base_score: float = Field(ge=0, le=10)
    german_required: Literal["not_required", "b1", "b2", "c1_c2"]
    seniority_cap_applied: bool
    tools_found: List[str]
    concerns: Optional[str]

class ScoredJob:
    """The final object used by the application."""
    def __init__(self, job: JobPost, llm_data: ScoredJobResponse, threshold: float):
        self.job = job
        self.base_score = llm_data.base_score
        self.german_required = llm_data.german_required
        
        # Calculate Penalty in Python (LLMs are bad at math)
        penalties = {"not_required": 0.0, "b1": 0.0, "b2": 1.5, "c1_c2": 3.0}
        self.german_penalty = penalties.get(llm_data.german_required, 0.0)
        
        # Final calculation
        self.score = round(max(0.0, self.base_score - self.german_penalty), 1)
        self.passed = self.score >= threshold
        
        # Metadata
        self.summary = f"Match: {llm_data.base_score} - Penalty: {self.german_penalty} | {llm_data.reasoning}"
        self.concerns = llm_data.concerns

def score_jobs(jobs: List[JobPost], config: AppConfig) -> tuple[List[ScoredJob], List[ScoredJob]]:
    client = Groq(api_key=config.groq_api_key)
    passed, rejected = [], []

    for i, job in enumerate(jobs):
        logger.info(f"[{i+1}/{len(jobs)}] Evaluating {job.title} @ {job.company}")

        try:
            # We focus the description on the first 4000 chars to save tokens 
            # while keeping core requirements intact.
            prompt = USER_PROMPT_TEMPLATE.format(
                title=job.title,
                company=job.company,
                location=job.location,
                description=job.description[:4000]
            )

            completion = client.chat.completions.create(
                model=config.groq_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1, # Low temperature for consistent scoring
                response_format={"type": "json_object"}
            )

            # Parse and Validate
            raw_content = completion.choices[0].message.content
            response_data = ScoredJobResponse.model_validate_json(raw_content)
            
            scored = ScoredJob(job, response_data, config.score_threshold)

            if scored.passed:
                passed.append(scored)
                logger.info(f"  ✓ PASSED: {scored.score}")
            else:
                rejected.append(scored)
                logger.info(f"  ✗ REJECTED: {scored.score} (Req: {scored.german_required})")

        except Exception as e:
            logger.error(f"  ! Error scoring {job.title}: {str(e)}")
            continue
        
        time.sleep(1.5) # Rate limit safety

    return passed, rejected