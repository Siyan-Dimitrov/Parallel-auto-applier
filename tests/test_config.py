"""Tests for config loading and validation."""
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from src.config import load_config, Config, JobPreferences, PersonalInfo


@pytest.fixture
def valid_config_data():
    return {
        "job_preferences": {
            "titles": ["Data Scientist", "ML Engineer"],
            "locations": ["Remote"],
            "min_match_score": 0.7,
        },
        "personal_info": {
            "full_name": "Test User",
            "email": "test@example.com",
            "phone": "555-1234",
            "linkedin_url": "https://linkedin.com/in/test",
            "website": "https://test.com",
            "current_company": "TestCorp",
            "years_experience": "5",
        },
        "ollama": {
            "model": "llama3.1",
            "base_url": "http://localhost:11434",
        },
        "browser": {
            "headless": True,
            "slow_mo": 100,
            "timeout": 10000,
        },
        "application": {
            "max_daily_applications": 10,
            "delay_between_applications": [5, 10],
            "generate_cover_letter": True,
            "resume_path": "resume/resume.pdf",
        },
        "logging": {
            "level": "DEBUG",
            "file": "test.log",
        },
    }


@pytest.fixture
def config_file(valid_config_data, tmp_path):
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(valid_config_data, f)
    return config_path


def test_load_valid_config(config_file):
    config = load_config(config_file)
    assert isinstance(config, Config)
    assert config.job_preferences.titles == ["Data Scientist", "ML Engineer"]
    assert config.personal_info.full_name == "Test User"
    assert config.personal_info.email == "test@example.com"
    assert config.ollama.model == "llama3.1"
    assert config.browser.headless is True
    assert config.application.max_daily_applications == 10


def test_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config(Path("/nonexistent/config.yaml"))


def test_config_missing_name(valid_config_data, tmp_path):
    valid_config_data["personal_info"]["full_name"] = ""
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(valid_config_data, f)
    with pytest.raises(ValueError, match="full_name"):
        load_config(config_path)


def test_config_missing_email(valid_config_data, tmp_path):
    valid_config_data["personal_info"]["email"] = ""
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(valid_config_data, f)
    with pytest.raises(ValueError, match="email"):
        load_config(config_path)


def test_config_no_titles(valid_config_data, tmp_path):
    valid_config_data["job_preferences"]["titles"] = []
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(valid_config_data, f)
    with pytest.raises(ValueError, match="titles"):
        load_config(config_path)


def test_config_invalid_score(valid_config_data, tmp_path):
    valid_config_data["job_preferences"]["min_match_score"] = 1.5
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(valid_config_data, f)
    with pytest.raises(ValueError, match="min_match_score"):
        load_config(config_path)


def test_config_defaults():
    """Test dataclass defaults are reasonable."""
    prefs = JobPreferences()
    assert prefs.min_match_score == 0.6
    assert prefs.locations == ["Remote"]

    personal = PersonalInfo()
    assert personal.full_name == ""
