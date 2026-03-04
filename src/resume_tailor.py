"""Resume Tailor — AI-powered resume tailoring with fabrication guard."""
from __future__ import annotations

import json
import re

import ollama as ollama_client

from src.config import Config, ProfileConfig
from src.utils.logging import get_logger


class ResumeTailor:
    """Tailors resumes to specific job descriptions while guarding against fabrication."""

    def __init__(self, config: Config):
        self.config = config
        self.profile = config.profile
        self.model = config.ollama.model
        self.client = ollama_client.Client(host=config.ollama.base_url)
        self.log = get_logger()

    def _chat(self, prompt: str, system: str = "") -> str:
        """Send a prompt to Ollama and return the response text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat(model=self.model, messages=messages)
        return response["message"]["content"]

    def tailor(
        self,
        original_resume: str,
        job_description: str,
        job_title: str,
        company: str,
    ) -> dict:
        """Tailor a resume for a specific job.

        Returns:
            dict with keys:
                - tailored_resume: str (the tailored text, or original if validation fails)
                - passed_validation: bool
                - validation_issues: list[str]
        """
        # Build skills boundary constraint
        all_skills = self.profile.skills_boundary.all_skills()
        skills_constraint = ""
        if all_skills:
            skills_constraint = (
                f"\nALLOWED SKILLS (only use these): {', '.join(all_skills)}"
            )

        # Build preserved facts
        facts_constraint = self._build_facts_constraint()

        system = (
            "You are a professional resume tailoring assistant. "
            "You rewrite resumes to better match specific job descriptions.\n\n"
            "STRICT RULES:\n"
            "- NEVER invent companies, degrees, certifications, or job titles\n"
            "- NEVER fabricate metrics, percentages, or achievements\n"
            "- MAY reorder sections to emphasize relevance\n"
            "- MAY rephrase bullet points to use job-relevant keywords\n"
            "- MAY adjust emphasis on existing skills\n"
            "- MUST preserve all dates, company names, school names, and degrees\n"
            "- MUST preserve the overall structure and format\n"
            f"{skills_constraint}"
            f"{facts_constraint}"
        )

        prompt = f"""Tailor this resume for the following job. Return ONLY the tailored resume text.

## Original Resume:
{original_resume[:4000]}

## Target Job Title: {job_title}
## Company: {company}
## Job Description:
{job_description[:3000]}

Return the tailored resume text only. No commentary or explanation."""

        try:
            tailored = self._chat(prompt, system)
        except Exception as e:
            self.log.error("Resume tailoring failed: %s", e)
            return {
                "tailored_resume": original_resume,
                "passed_validation": False,
                "validation_issues": [f"Generation error: {e}"],
            }

        # Run fabrication guard
        issues = self._validate_tailored_resume(original_resume, tailored)

        if issues:
            self.log.warning("Tailored resume failed validation: %s", "; ".join(issues))
            return {
                "tailored_resume": original_resume,
                "passed_validation": False,
                "validation_issues": issues,
            }

        return {
            "tailored_resume": tailored,
            "passed_validation": True,
            "validation_issues": [],
        }

    def _build_facts_constraint(self) -> str:
        """Build the preserved facts section of the prompt."""
        parts = []
        rf = self.profile.resume_facts

        if rf.preserved_companies:
            parts.append(f"PRESERVED COMPANIES (must appear): {', '.join(rf.preserved_companies)}")
        if rf.preserved_projects:
            parts.append(f"PRESERVED PROJECTS (must appear): {', '.join(rf.preserved_projects)}")
        if rf.preserved_school:
            parts.append(f"PRESERVED SCHOOLS (must appear): {', '.join(rf.preserved_school)}")
        if rf.real_metrics:
            parts.append(f"REAL METRICS (use only these): {'; '.join(rf.real_metrics)}")

        if parts:
            return "\n" + "\n".join(parts)
        return ""

    def _validate_tailored_resume(self, original: str, tailored: str) -> list[str]:
        """Two-layer fabrication guard: rule-based + LLM judge."""
        issues = []

        # Layer 1: Rule-based checks
        rule_issues = self._rule_based_validation(original, tailored)
        issues.extend(rule_issues)

        # Layer 2: LLM judge (only if rule-based passed)
        if not rule_issues:
            llm_issues = self._llm_judge_validation(original, tailored)
            issues.extend(llm_issues)

        return issues

    def _rule_based_validation(self, original: str, tailored: str) -> list[str]:
        """Check that preserved facts are still present in the tailored version."""
        issues = []
        tailored_lower = tailored.lower()

        # Check preserved companies
        for company in self.profile.resume_facts.preserved_companies:
            if company.lower() not in tailored_lower:
                issues.append(f"Missing preserved company: '{company}'")

        # Check preserved schools
        for school in self.profile.resume_facts.preserved_school:
            if school.lower() not in tailored_lower:
                issues.append(f"Missing preserved school: '{school}'")

        # Basic sanity: tailored shouldn't be dramatically shorter or longer
        orig_words = len(original.split())
        tailored_words = len(tailored.split())
        if orig_words > 0:
            ratio = tailored_words / orig_words
            if ratio < 0.5:
                issues.append(f"Tailored resume is too short ({tailored_words} vs {orig_words} words)")
            if ratio > 2.0:
                issues.append(f"Tailored resume is too long ({tailored_words} vs {orig_words} words)")

        return issues

    def _llm_judge_validation(self, original: str, tailored: str) -> list[str]:
        """Use LLM to detect fabrication by comparing original and tailored resumes."""
        system = (
            "You are a resume validation judge. Compare the original and tailored resumes. "
            "Check for FABRICATION: invented companies, invented degrees, invented metrics, "
            "or skills the candidate clearly doesn't have based on the original. "
            "Return ONLY valid JSON: {\"fabricated\": true/false, \"issues\": [\"issue1\", ...]}"
        )

        prompt = f"""Compare these two resumes and check for fabrication.

## Original Resume:
{original[:2500]}

## Tailored Resume:
{tailored[:2500]}

Check for:
1. Companies or employers that don't appear in the original
2. Degrees or certifications not in the original
3. Metrics or percentages not in the original
4. Skills claimed that aren't supported by the original

Return ONLY JSON: {{"fabricated": true/false, "issues": ["issue1", ...]}}"""

        try:
            response = self._chat(prompt, system)
            # Parse JSON from response
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                if data.get("fabricated", False):
                    return data.get("issues", ["LLM detected fabrication"])
        except Exception as e:
            self.log.warning("LLM judge validation failed: %s — skipping LLM check", e)

        return []
