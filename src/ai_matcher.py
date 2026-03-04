from __future__ import annotations

import json
import re

import ollama as ollama_client

from src.config import Config, ProfileConfig
from src.utils.logging import get_logger


COVER_LETTER_BANNED_WORDS = [
    "synergy", "leverage", "passionate", "excited", "thrilled",
    "delighted", "eager beaver", "game-changer", "rockstar",
    "ninja", "guru", "wizard", "unicorn", "disrupt",
    "circle back", "move the needle", "paradigm shift",
    "deep dive", "thought leader", "best-in-class",
]

MAX_COVER_LETTER_RETRIES = 3
MAX_COVER_LETTER_WORDS = 400


class AIMatcher:
    """Uses Ollama to score job fit, generate cover letters, and identify form fields."""

    def __init__(self, config: Config):
        self.config = config
        self.model = config.ollama.model
        self.match_model = config.ollama.match_model
        self.client = ollama_client.Client(host=config.ollama.base_url)
        self.log = get_logger()

    def _chat(self, prompt: str, system: str = "", model: str | None = None) -> str:
        """Send a prompt to Ollama and return the response text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat(model=model or self.model, messages=messages)
        return response["message"]["content"]

    def score_job(self, job_description: str, resume_text: str, job_location: str = "") -> dict:
        """Score how well a job matches the candidate's resume.

        Returns: {"score": 0.0-1.0, "reasoning": "..."}
        """
        system = (
            "You are a job matching assistant. You evaluate how well a candidate's resume "
            "matches a job description. Return ONLY valid JSON with two fields: "
            '"score" (a float from 0.0 to 1.0) and "reasoning" (a brief explanation). '
            "Consider: skill overlap, experience level, domain relevance, and location fit. "
            "If the job location does not match any of the candidate's preferred locations, "
            "reduce the score significantly."
        )

        location_section = ""
        if job_location:
            location_section = f"\n## Job Location:\n{job_location}\n"

        prompt = f"""Rate the match between this resume and job description.

## Resume:
{resume_text[:3000]}

## Job Description:
{job_description[:3000]}
{location_section}
## Target Roles:
{', '.join(self.config.job_preferences.titles)}

## Preferred Locations:
{', '.join(self.config.job_preferences.locations)}

Return ONLY JSON: {{"score": 0.0-1.0, "reasoning": "..."}}"""

        try:
            response = self._chat(prompt, system, model=self.match_model)
            return self._parse_score_response(response)
        except Exception as e:
            self.log.error("AI scoring failed: %s", e)
            return {"score": 0.0, "reasoning": f"Scoring error: {e}"}

    def score_jobs_batch(self, jobs: list[dict], resume_text: str) -> list[dict]:
        """Score multiple jobs in a single LLM call.

        Args:
            jobs: List of dicts with keys: id, title, description, location.
            resume_text: Parsed resume text.

        Returns:
            List of dicts: [{"job_id": int, "score": float, "reasoning": str}, ...]
            Jobs that fail parsing get score 0.0.
        """
        system = (
            "You are a job matching assistant. You evaluate how well a candidate's resume "
            "matches multiple job descriptions. For EACH job, return a JSON object with "
            '"job_index" (the number shown), "score" (float 0.0-1.0), and "reasoning" (brief explanation). '
            "Consider: skill overlap, experience level, domain relevance, and location fit. "
            "If a job's location does not match the candidate's preferred locations, reduce its score significantly.\n\n"
            "Return ONLY a JSON array of objects. No other text."
        )

        # Build numbered job entries — truncate descriptions to keep prompt manageable
        job_entries = []
        for i, job in enumerate(jobs, 1):
            desc = (job.get("description") or job.get("title", ""))[:1500]
            title = job.get("title", "Unknown")
            location = job.get("location", "")
            job_entries.append(
                f"### Job {i}: {title}\n"
                f"Location: {location}\n"
                f"Description: {desc}"
            )

        prompt = f"""Rate the match between this resume and each job below.

## Resume:
{resume_text[:3000]}

## Target Roles:
{', '.join(self.config.job_preferences.titles)}

## Preferred Locations:
{', '.join(self.config.job_preferences.locations)}

## Jobs to Score:

{chr(10).join(job_entries)}

Return ONLY a JSON array: [{{"job_index": 1, "score": 0.85, "reasoning": "..."}}, ...]"""

        try:
            response = self._chat(prompt, system, model=self.match_model)
            parsed = self._parse_batch_response(response, len(jobs))
        except Exception as e:
            self.log.error("Batch scoring failed: %s — falling back to individual scoring", e)
            parsed = None

        # If batch parse succeeded, map results back to job IDs
        if parsed and len(parsed) == len(jobs):
            results = []
            for i, job in enumerate(jobs):
                entry = parsed[i]
                results.append({
                    "job_id": job["id"],
                    "score": entry["score"],
                    "reasoning": entry["reasoning"],
                })
            return results

        # Fallback: score individually
        self.log.warning("Batch parse incomplete (%s/%s) — scoring remaining individually",
                         len(parsed) if parsed else 0, len(jobs))
        results = []
        scored_indices = set()
        if parsed:
            for entry in parsed:
                idx = entry.get("_index")
                if idx is not None and 0 <= idx < len(jobs):
                    results.append({
                        "job_id": jobs[idx]["id"],
                        "score": entry["score"],
                        "reasoning": entry["reasoning"],
                    })
                    scored_indices.add(idx)

        for i, job in enumerate(jobs):
            if i in scored_indices:
                continue
            desc = job.get("description") or job.get("title", "")
            result = self.score_job(desc, resume_text, job_location=job.get("location", ""))
            results.append({
                "job_id": job["id"],
                "score": result["score"],
                "reasoning": result["reasoning"],
            })
        return results

    def _parse_batch_response(self, response: str, expected_count: int) -> list[dict] | None:
        """Parse a JSON array of score objects from batch response."""
        # Try direct parse
        for text in [response, self._extract_json_block(response)]:
            if not text:
                continue
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    return self._normalize_batch_results(data, expected_count)
            except (json.JSONDecodeError, KeyError):
                pass

        # Try to find a JSON array in the response
        arr_match = re.search(r"\[[\s\S]*\]", response)
        if arr_match:
            try:
                data = json.loads(arr_match.group(0))
                if isinstance(data, list):
                    return self._normalize_batch_results(data, expected_count)
            except (json.JSONDecodeError, KeyError):
                pass

        return None

    @staticmethod
    def _extract_json_block(text: str) -> str | None:
        """Extract JSON from markdown code block."""
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        return m.group(1) if m else None

    @staticmethod
    def _normalize_batch_results(data: list, expected_count: int) -> list[dict]:
        """Normalize parsed batch results into a consistent format."""
        results = []
        for item in data:
            if not isinstance(item, dict) or "score" not in item:
                continue
            idx = item.get("job_index", len(results) + 1) - 1  # 1-based → 0-based
            results.append({
                "_index": idx,
                "score": max(0.0, min(1.0, float(item["score"]))),
                "reasoning": str(item.get("reasoning", "")),
            })
        return results

    def _parse_score_response(self, response: str) -> dict:
        """Parse the JSON response from the scoring prompt."""
        # Try direct JSON parse
        try:
            data = json.loads(response)
            return {
                "score": max(0.0, min(1.0, float(data["score"]))),
                "reasoning": str(data.get("reasoning", "")),
            }
        except (json.JSONDecodeError, KeyError):
            pass

        # Try extracting JSON from markdown code block
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                return {
                    "score": max(0.0, min(1.0, float(data["score"]))),
                    "reasoning": str(data.get("reasoning", "")),
                }
            except (json.JSONDecodeError, KeyError):
                pass

        # Try to find any JSON object in the response
        json_match = re.search(r"\{[^{}]*\"score\"[^{}]*\}", response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                return {
                    "score": max(0.0, min(1.0, float(data["score"]))),
                    "reasoning": str(data.get("reasoning", "")),
                }
            except (json.JSONDecodeError, KeyError):
                pass

        self.log.warning("Could not parse AI score response: %s", response[:200])
        return {"score": 0.0, "reasoning": "Failed to parse AI response"}

    def generate_cover_letter(
        self,
        job_description: str,
        resume_text: str,
        company: str,
        title: str,
        profile: ProfileConfig | None = None,
    ) -> str:
        """Generate a tailored cover letter with validation and retry logic."""
        personal = self.config.personal_info

        # Build skills constraint if profile available
        skills_constraint = ""
        metrics_reference = ""
        if profile:
            all_skills = profile.skills_boundary.all_skills()
            if all_skills:
                skills_constraint = (
                    f"\n\nSKILLS CONSTRAINT: You may ONLY mention these skills: "
                    f"{', '.join(all_skills)}. Do NOT invent or assume other skills."
                )
            if profile.resume_facts.real_metrics:
                metrics_reference = (
                    f"\n\nREAL METRICS for reference (use where relevant): "
                    f"{'; '.join(profile.resume_facts.real_metrics)}"
                )

        system = (
            "You are a professional cover letter writer. Write exactly 3 paragraphs:\n"
            "1. Opening: Lead with relevant work experience (NOT 'I am writing to...')\n"
            "2. Achievements: Include specific, quantifiable metrics from the resume\n"
            "3. Company-specific: Show knowledge of the company and why you're a fit\n\n"
            "Rules:\n"
            "- Under 400 words total\n"
            "- No placeholder brackets like [X], {company}, or {role}\n"
            "- No generic filler phrases\n"
            "- Be specific to the role and company\n"
            f"- Do NOT use these words: {', '.join(COVER_LETTER_BANNED_WORDS[:10])}"
            f"{skills_constraint}{metrics_reference}"
        )

        prompt = f"""Write a cover letter for this application.

## Applicant:
Name: {personal.full_name}
Current Company: {personal.current_company}
Years of Experience: {personal.years_experience}

## Resume Summary:
{resume_text[:2000]}

## Job Title: {title}
## Company: {company}
## Job Description:
{job_description[:2000]}

Write a professional 3-paragraph cover letter. No placeholder brackets. Under 400 words."""

        for attempt in range(MAX_COVER_LETTER_RETRIES):
            try:
                letter = self._chat(prompt, system)
                issues = self._validate_cover_letter(letter)
                if not issues:
                    return letter
                self.log.warning(
                    "Cover letter attempt %d/%d failed validation: %s",
                    attempt + 1, MAX_COVER_LETTER_RETRIES, "; ".join(issues),
                )
                # Add validation feedback to prompt for retry
                prompt += f"\n\nPREVIOUS ATTEMPT HAD ISSUES: {'; '.join(issues)}. Please fix them."
            except Exception as e:
                self.log.error("Cover letter generation attempt %d failed: %s", attempt + 1, e)

        # Return last attempt even if validation failed
        self.log.warning("Cover letter validation failed after %d attempts, using last result.", MAX_COVER_LETTER_RETRIES)
        return letter if "letter" in dir() else ""

    def _validate_cover_letter(self, letter: str) -> list[str]:
        """Validate a cover letter. Returns list of issues (empty = valid)."""
        issues = []

        # Check for banned words
        letter_lower = letter.lower()
        for word in COVER_LETTER_BANNED_WORDS:
            if word.lower() in letter_lower:
                issues.append(f"Contains banned word: '{word}'")

        # Check for placeholder brackets
        if re.search(r"\[[\w\s]+\]", letter):
            issues.append("Contains placeholder brackets [...]")
        if re.search(r"\{[\w\s]+\}", letter):
            issues.append("Contains placeholder braces {...}")

        # Check word count
        word_count = len(letter.split())
        if word_count > MAX_COVER_LETTER_WORDS:
            issues.append(f"Too long: {word_count} words (max {MAX_COVER_LETTER_WORDS})")

        # Check for generic opening
        if letter.strip().lower().startswith("i am writing to"):
            issues.append("Starts with 'I am writing to...'")

        return issues

    def identify_form_fields(self, form_html: str, personal_info: dict) -> dict:
        """Use AI to map form fields to personal info values.

        Returns a dict of CSS selector -> value to fill.
        """
        system = (
            "You are a web form analysis assistant. Given HTML of an application form "
            "and applicant info, return a JSON object mapping CSS selectors to the values "
            "that should be filled in. Only include fields you're confident about."
        )

        prompt = f"""Analyze this job application form and map fields to values.

## Applicant Info:
{json.dumps(personal_info, indent=2)}

## Form HTML:
{form_html[:4000]}

Return ONLY a JSON object mapping CSS selectors to values, like:
{{"#first_name": "John", "#email": "john@example.com", "input[name='phone']": "555-1234"}}"""

        try:
            response = self._chat(prompt, system)
            # Extract JSON from response
            json_match = re.search(r"\{[^{}]*\}", response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
        except Exception as e:
            self.log.error("Form field identification failed: %s", e)

        return {}
