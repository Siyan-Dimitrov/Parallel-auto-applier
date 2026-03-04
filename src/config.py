from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.utils.logging import get_logger

CONFIG_PATH = Path("config/config.yaml")
EXAMPLE_CONFIG_PATH = Path("config/config.example.yaml")
PROFILE_PATH = Path("config/profile.json")
EMPLOYERS_PATH = Path("config/employers.yaml")
SITES_PATH = Path("config/sites.yaml")


@dataclass
class JobPreferences:
    titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=lambda: ["Remote"])
    min_match_score: float = 0.6
    results_wanted: int = 50
    hours_old: int = 72
    is_remote: bool = False
    job_type: str = ""          # fulltime, parttime, contract, internship, or "" for any
    country_indeed: str = "UK"  # Country for Indeed searches
    distance: int = 50          # Distance in miles from location
    min_salary: int | None = None  # Minimum annual salary — jobs below this are filtered out


@dataclass
class PersonalInfo:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    website: str = ""
    current_company: str = ""
    years_experience: str = ""
    password: str = ""          # For creating accounts on job sites
    notice_period: str = "2 weeks"  # e.g. "Immediately", "2 weeks", "1 month", "3 months"
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""


@dataclass
class OllamaConfig:
    model: str = "llama3.1"
    match_model: str = "qwen2.5:14b"
    vision_model: str = "kimi-k2.5:cloud"
    base_url: str = "http://localhost:11434"


@dataclass
class BrowserConfig:
    headless: bool = False
    slow_mo: int = 500
    timeout: int = 30000


@dataclass
class ApplicationConfig:
    max_daily_applications: int = 50
    delay_between_applications: list[int] = field(default_factory=lambda: [30, 90])
    generate_cover_letter: bool = True
    resume_path: str = "resume/resume.pdf"
    num_workers: int = 1  # Parallel application workers (each gets its own Chrome instance)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "bot.log"


# ── Phase 1: Profile ────────────────────────────────────────────────────

@dataclass
class SkillsBoundary:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    devops: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def all_skills(self) -> list[str]:
        return self.languages + self.frameworks + self.devops + self.databases + self.tools


@dataclass
class ResumeFacts:
    preserved_companies: list[str] = field(default_factory=list)
    preserved_projects: list[str] = field(default_factory=list)
    preserved_school: list[str] = field(default_factory=list)
    real_metrics: list[str] = field(default_factory=list)


@dataclass
class WorkAuthorization:
    legally_authorized: bool = True
    require_sponsorship: bool = False


@dataclass
class Compensation:
    salary_expectation: str = ""
    range_min: int | None = None
    range_max: int | None = None
    currency: str = "GBP"


@dataclass
class EEOVoluntary:
    gender: str | None = None
    race_ethnicity: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None


@dataclass
class ProfileConfig:
    skills_boundary: SkillsBoundary = field(default_factory=SkillsBoundary)
    resume_facts: ResumeFacts = field(default_factory=ResumeFacts)
    work_authorization: WorkAuthorization = field(default_factory=WorkAuthorization)
    compensation: Compensation = field(default_factory=Compensation)
    eeo_voluntary: EEOVoluntary = field(default_factory=EEOVoluntary)


# ── CDP & CAPTCHA ─────────────────────────────────────────────────────

@dataclass
class EmailConfig:
    imap_host: str = "imap.gmail.com"
    email: str = ""
    app_password: str = ""
    enabled: bool = False


@dataclass
class CdpConfig:
    chrome_path: str = ""       # Auto-detected when empty
    base_port: int = 9222
    profile_dir: str = "data/chrome_profiles"


@dataclass
class CaptchaConfig:
    provider: str = "capsolver"
    api_key: str = ""
    enabled: bool = False


# ── API Scrapers ─────────────────────────────────────────────────────

@dataclass
class AdzunaConfig:
    app_id: str = ""
    app_key: str = ""


@dataclass
class CareerjetConfig:
    affid: str = ""  # Affiliate ID from careerjet.com/partners/api


# ── Phase 3: Workday Employers ──────────────────────────────────────────

@dataclass
class WorkdayEmployer:
    name: str = ""
    tenant: str = ""
    site_id: str = "External"
    base_url: str = ""


@dataclass
class EmployersConfig:
    workday_employers: list[WorkdayEmployer] = field(default_factory=list)


# ── Phase 6: Career Sites ──────────────────────────────────────────────

@dataclass
class CareerSite:
    name: str = ""
    url: str = ""


@dataclass
class SitesConfig:
    career_pages: list[CareerSite] = field(default_factory=list)


# ── Main Config ─────────────────────────────────────────────────────────

@dataclass
class Config:
    job_preferences: JobPreferences = field(default_factory=JobPreferences)
    personal_info: PersonalInfo = field(default_factory=PersonalInfo)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    application: ApplicationConfig = field(default_factory=ApplicationConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    employers: EmployersConfig = field(default_factory=EmployersConfig)
    sites: SitesConfig = field(default_factory=SitesConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    cdp: CdpConfig = field(default_factory=CdpConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    adzuna: AdzunaConfig = field(default_factory=AdzunaConfig)
    careerjet: CareerjetConfig = field(default_factory=CareerjetConfig)


# ── Loaders ─────────────────────────────────────────────────────────────

def load_profile(path: Path | None = None) -> ProfileConfig:
    """Load profile from JSON. Returns defaults if file doesn't exist."""
    profile_path = path or PROFILE_PATH
    if not profile_path.exists():
        return ProfileConfig()

    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        get_logger().warning("Could not parse %s — using default profile.", profile_path)
        return ProfileConfig()

    return ProfileConfig(
        skills_boundary=SkillsBoundary(**raw.get("skills_boundary", {})),
        resume_facts=ResumeFacts(**raw.get("resume_facts", {})),
        work_authorization=WorkAuthorization(**raw.get("work_authorization", {})),
        compensation=Compensation(**raw.get("compensation", {})),
        eeo_voluntary=EEOVoluntary(**raw.get("eeo_voluntary", {})),
    )


def load_employers(path: Path | None = None) -> EmployersConfig:
    """Load Workday employer list from YAML. Returns empty if file doesn't exist."""
    employers_path = path or EMPLOYERS_PATH
    if not employers_path.exists():
        return EmployersConfig()

    try:
        with open(employers_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        get_logger().warning("Could not parse %s — using empty employer list.", employers_path)
        return EmployersConfig()

    employers = []
    for item in raw.get("workday_employers", []):
        employers.append(WorkdayEmployer(**item))
    return EmployersConfig(workday_employers=employers)


def load_sites(path: Path | None = None) -> SitesConfig:
    """Load career sites from YAML. Returns empty if file doesn't exist."""
    sites_path = path or SITES_PATH
    if not sites_path.exists():
        return SitesConfig()

    try:
        with open(sites_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        get_logger().warning("Could not parse %s — using empty sites list.", sites_path)
        return SitesConfig()

    pages = []
    for item in raw.get("career_pages", []):
        pages.append(CareerSite(**item))
    return SitesConfig(career_pages=pages)


def load_config(path: Path | None = None, validate: bool = True) -> Config:
    """Load and validate configuration from YAML file."""
    config_path = path or CONFIG_PATH
    log = get_logger()

    if not config_path.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"Config file not found at {config_path}. "
                f"Copy {EXAMPLE_CONFIG_PATH} to {config_path} and fill in your details."
            )
        raise FileNotFoundError(f"Config file not found at {config_path}.")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = Config(
        job_preferences=JobPreferences(**raw.get("job_preferences", {})),
        personal_info=PersonalInfo(**raw.get("personal_info", {})),
        ollama=OllamaConfig(**raw.get("ollama", {})),
        browser=BrowserConfig(**raw.get("browser", {})),
        application=ApplicationConfig(**raw.get("application", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
        profile=load_profile(),
        employers=load_employers(),
        sites=load_sites(),
        email=EmailConfig(**raw.get("email", {})),
        cdp=CdpConfig(**raw.get("cdp", {})),
        captcha=CaptchaConfig(**raw.get("captcha", {})),
        adzuna=AdzunaConfig(**raw.get("adzuna", {})),
        careerjet=CareerjetConfig(**raw.get("careerjet", {})),
    )

    if validate:
        _validate(config)
    log.info("Configuration loaded from %s", config_path)
    return config


def _validate(config: Config) -> None:
    """Validate config values, raise on critical issues."""
    if not config.job_preferences.titles:
        raise ValueError("job_preferences.titles must contain at least one job title.")
    if not config.personal_info.full_name:
        raise ValueError("personal_info.full_name is required.")
    if not config.personal_info.email:
        raise ValueError("personal_info.email is required.")
    if not 0 <= config.job_preferences.min_match_score <= 1:
        raise ValueError("min_match_score must be between 0 and 1.")
    resume = Path(config.application.resume_path)
    if not resume.exists():
        get_logger().warning("Resume not found at %s — upload will be skipped.", resume)
