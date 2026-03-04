"""Tests for ResumeTailor validation logic (no Ollama required)."""
from __future__ import annotations

import pytest

from src.config import (
    Config, ProfileConfig, SkillsBoundary, ResumeFacts,
    OllamaConfig, JobPreferences, PersonalInfo,
)
from src.resume_tailor import ResumeTailor


def _make_config(
    preserved_companies: list[str] | None = None,
    preserved_school: list[str] | None = None,
    skills: list[str] | None = None,
) -> Config:
    """Create a minimal Config with profile for testing."""
    return Config(
        job_preferences=JobPreferences(titles=["Data Scientist"]),
        personal_info=PersonalInfo(full_name="Test User", email="test@test.com"),
        ollama=OllamaConfig(model="test-model"),
        profile=ProfileConfig(
            skills_boundary=SkillsBoundary(languages=skills or []),
            resume_facts=ResumeFacts(
                preserved_companies=preserved_companies or [],
                preserved_school=preserved_school or [],
            ),
        ),
    )


class TestRuleBasedValidation:
    """Test the rule-based fabrication guard (no LLM needed)."""

    def test_preserved_company_present(self):
        config = _make_config(preserved_companies=["Acme Corp"])
        tailor = ResumeTailor(config)
        original = "Worked at Acme Corp for 3 years as a data scientist."
        tailored = "Senior data scientist at Acme Corp with 3 years experience."
        issues = tailor._rule_based_validation(original, tailored)
        assert issues == []

    def test_preserved_company_missing(self):
        config = _make_config(preserved_companies=["Acme Corp"])
        tailor = ResumeTailor(config)
        original = "Worked at Acme Corp for 3 years."
        tailored = "Senior data scientist with 3 years experience."
        issues = tailor._rule_based_validation(original, tailored)
        assert len(issues) == 1
        assert "Acme Corp" in issues[0]

    def test_preserved_school_present(self):
        config = _make_config(preserved_school=["MIT"])
        tailor = ResumeTailor(config)
        original = "BSc in CS from MIT."
        tailored = "Computer Science degree from MIT."
        issues = tailor._rule_based_validation(original, tailored)
        assert issues == []

    def test_preserved_school_missing(self):
        config = _make_config(preserved_school=["MIT"])
        tailor = ResumeTailor(config)
        original = "BSc in CS from MIT."
        tailored = "Computer Science degree from Stanford."
        issues = tailor._rule_based_validation(original, tailored)
        assert len(issues) == 1
        assert "MIT" in issues[0]

    def test_tailored_too_short(self):
        config = _make_config()
        tailor = ResumeTailor(config)
        original = "This is a long resume with many words. " * 50
        tailored = "Short version."
        issues = tailor._rule_based_validation(original, tailored)
        assert any("too short" in i for i in issues)

    def test_tailored_too_long(self):
        config = _make_config()
        tailor = ResumeTailor(config)
        original = "Short resume."
        tailored = "This is a much longer tailored resume with many words. " * 50
        issues = tailor._rule_based_validation(original, tailored)
        assert any("too long" in i for i in issues)

    def test_reasonable_length_passes(self):
        config = _make_config()
        tailor = ResumeTailor(config)
        words = "word " * 100
        original = words
        tailored = words  # Same length
        issues = tailor._rule_based_validation(original, tailored)
        assert issues == []

    def test_case_insensitive_company_check(self):
        config = _make_config(preserved_companies=["ACME Corp"])
        tailor = ResumeTailor(config)
        original = "Worked at ACME Corp."
        tailored = "Worked at acme corp as a data scientist."
        issues = tailor._rule_based_validation(original, tailored)
        assert issues == []

    def test_multiple_preserved_facts(self):
        config = _make_config(
            preserved_companies=["Acme Corp", "Beta Inc"],
            preserved_school=["MIT"],
        )
        tailor = ResumeTailor(config)
        original = "Acme Corp, Beta Inc, MIT graduate."
        tailored = "MIT graduate. Acme Corp experience."
        issues = tailor._rule_based_validation(original, tailored)
        assert len(issues) == 1
        assert "Beta Inc" in issues[0]


class TestFactsConstraint:
    """Test the facts constraint builder."""

    def test_empty_profile(self):
        config = _make_config()
        tailor = ResumeTailor(config)
        result = tailor._build_facts_constraint()
        assert result == ""

    def test_with_companies(self):
        config = _make_config(preserved_companies=["Acme Corp"])
        tailor = ResumeTailor(config)
        result = tailor._build_facts_constraint()
        assert "Acme Corp" in result
        assert "PRESERVED COMPANIES" in result

    def test_with_schools(self):
        config = _make_config(preserved_school=["MIT"])
        tailor = ResumeTailor(config)
        result = tailor._build_facts_constraint()
        assert "MIT" in result
        assert "PRESERVED SCHOOLS" in result
