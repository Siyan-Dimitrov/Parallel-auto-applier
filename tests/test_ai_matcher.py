"""Tests for AI matcher response parsing (no Ollama required)."""
import pytest

from src.ai_matcher import AIMatcher
from src.config import Config, JobPreferences, PersonalInfo, OllamaConfig


@pytest.fixture
def matcher():
    """Create an AIMatcher with default config (doesn't connect to Ollama)."""
    config = Config(
        job_preferences=JobPreferences(titles=["Data Scientist"]),
        personal_info=PersonalInfo(full_name="Test", email="t@t.com"),
        ollama=OllamaConfig(),
    )
    return AIMatcher(config)


class TestScoreParsing:
    def test_parse_clean_json(self, matcher):
        result = matcher._parse_score_response('{"score": 0.85, "reasoning": "Great match"}')
        assert result["score"] == 0.85
        assert result["reasoning"] == "Great match"

    def test_parse_json_in_code_block(self, matcher):
        response = '```json\n{"score": 0.7, "reasoning": "Good fit"}\n```'
        result = matcher._parse_score_response(response)
        assert result["score"] == 0.7
        assert result["reasoning"] == "Good fit"

    def test_parse_json_with_surrounding_text(self, matcher):
        response = 'Here is my analysis:\n{"score": 0.6, "reasoning": "Decent match"}\nHope that helps!'
        result = matcher._parse_score_response(response)
        assert result["score"] == 0.6

    def test_score_clamped_to_max(self, matcher):
        result = matcher._parse_score_response('{"score": 1.5, "reasoning": "over"}')
        assert result["score"] == 1.0

    def test_score_clamped_to_min(self, matcher):
        result = matcher._parse_score_response('{"score": -0.3, "reasoning": "under"}')
        assert result["score"] == 0.0

    def test_unparseable_response(self, matcher):
        result = matcher._parse_score_response("I think this is a good match but no JSON here")
        assert result["score"] == 0.0
        assert "Failed to parse" in result["reasoning"]

    def test_parse_code_block_no_lang(self, matcher):
        response = '```\n{"score": 0.9, "reasoning": "Excellent"}\n```'
        result = matcher._parse_score_response(response)
        assert result["score"] == 0.9
