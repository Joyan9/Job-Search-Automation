import logging
import statistics
import time
from typing import Literal, List, Optional
from pydantic import BaseModel, Field
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
    def __init__(
        self,
        job: JobPost,
        llm_data: ScoredJobResponse,
        threshold: float,
        confidence: str,
        models_used: list[str],
        model_scores: list[float],
    ):
        self.job = job
        self.base_score = llm_data.base_score
        self.german_required = llm_data.german_required
        self.tools_found = llm_data.tools_found
        self.models_used = models_used
        self.model_scores = model_scores
        self.confidence = confidence
        
        # Calculate Penalty in Python (LLMs are bad at math)
        penalties = {"not_required": 0.0, "b1": 0.0, "b2": 1.5, "c1_c2": 3.0}
        self.german_penalty = penalties.get(llm_data.german_required, 0.0)
        
        # Final calculation
        self.score = round(max(0.0, self.base_score - self.german_penalty), 1)
        self.passed = self.score >= threshold
        
        # Metadata
        self.summary = f"Match: {llm_data.base_score} - Penalty: {self.german_penalty} | {llm_data.reasoning}"
        self.concerns = llm_data.concerns


def _build_prompt(job: JobPost) -> str:
    return USER_PROMPT_TEMPLATE.format(
        title=job.title,
        company=job.company,
        location=job.location,
        description=job.description[:4000],
    )


def _score_once(client: Groq, model: str, prompt: str, temperature: float) -> ScoredJobResponse:
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    raw_content = completion.choices[0].message.content
    return ScoredJobResponse.model_validate_json(raw_content)


def _consensus_german_requirement(responses: list[ScoredJobResponse]) -> str:
    penalties = {"not_required": 0.0, "b1": 0.0, "b2": 1.5, "c1_c2": 3.0}
    counts: dict[str, int] = {}
    for response in responses:
        counts[response.german_required] = counts.get(response.german_required, 0) + 1

    top_count = max(counts.values())
    top_values = [k for k, v in counts.items() if v == top_count]
    if len(top_values) == 1:
        return top_values[0]
    return sorted(top_values, key=lambda k: penalties[k], reverse=True)[0]


def _confidence_from_scores(scores: list[float]) -> str:
    if len(scores) <= 1:
        return "medium"
    spread = statistics.pstdev(scores)
    if spread <= 0.75:
        return "high"
    if spread <= 1.5:
        return "medium"
    return "low"


def _build_consensus_response(responses: list[ScoredJobResponse]) -> ScoredJobResponse:
    avg_base_score = round(sum(r.base_score for r in responses) / len(responses), 1)
    german_required = _consensus_german_requirement(responses)
    seniority_cap_applied = any(r.seniority_cap_applied for r in responses)

    tools = sorted({tool for response in responses for tool in response.tools_found})
    concerns = [c for c in (r.concerns for r in responses) if c and c.lower() != "none"]
    concern_text = concerns[0] if concerns else "None"
    reasoning = " | ".join(r.reasoning for r in responses[:2])

    return ScoredJobResponse(
        reasoning=reasoning,
        base_score=avg_base_score,
        german_required=german_required,
        seniority_cap_applied=seniority_cap_applied,
        tools_found=tools,
        concerns=concern_text,
    )

def score_jobs(jobs: List[JobPost], config: AppConfig) -> tuple[List[ScoredJob], List[ScoredJob]]:
    client = Groq(api_key=config.groq_api_key)
    models = config.consensus_models or [config.groq_model]
    passed, rejected = [], []

    for i, job in enumerate(jobs):
        logger.info(f"[{i+1}/{len(jobs)}] Evaluating {job.title} @ {job.company}")

        try:
            prompt = _build_prompt(job)
            responses: list[ScoredJobResponse] = []
            models_used: list[str] = []
            model_scores: list[float] = []

            for model in models:
                try:
                    response_data = _score_once(client, model, prompt, config.groq_temperature)
                    responses.append(response_data)
                    models_used.append(model)
                    model_scores.append(round(response_data.base_score, 1))
                except Exception as model_error:
                    logger.warning("  Model failed (%s): %s", model, model_error)

            if not responses:
                logger.error("  ! All models failed for %s", job.title)
                continue

            consensus = _build_consensus_response(responses)
            confidence = _confidence_from_scores(model_scores)
            scored = ScoredJob(
                job,
                consensus,
                config.score_threshold,
                confidence=confidence,
                models_used=models_used,
                model_scores=model_scores,
            )

            if scored.passed:
                passed.append(scored)
                logger.info(
                    "  ✓ PASSED: %s | confidence=%s | model_scores=%s",
                    scored.score,
                    scored.confidence,
                    scored.model_scores,
                )
            else:
                rejected.append(scored)
                logger.info(
                    "  ✗ REJECTED: %s (Req: %s) | confidence=%s | model_scores=%s",
                    scored.score,
                    scored.german_required,
                    scored.confidence,
                    scored.model_scores,
                )

        except Exception as e:
            logger.error(f"  ! Error scoring {job.title}: {str(e)}")
            continue
        
        time.sleep(1.5) # Rate limit safety

    return passed, rejected