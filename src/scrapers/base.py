from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.browser import BrowserManager
from src.config import Config
from src.database import Database
from src.utils.logging import get_logger


# Country name aliases for location matching
_COUNTRY_ALIASES: dict[str, list[str]] = {
    "uk": ["united kingdom", "england", "scotland", "wales", "northern ireland", "britain", "gb", "great britain"],
    "usa": ["united states", "us", "america", "u.s.", "u.s.a."],
    "uae": ["united arab emirates", "dubai", "abu dhabi"],
    "de": ["germany", "deutschland"],
    "fr": ["france"],
    "nl": ["netherlands", "holland"],
    "ca": ["canada"],
    "au": ["australia"],
    "in": ["india"],
    "sg": ["singapore"],
    "ie": ["ireland"],
    "ch": ["switzerland"],
    "se": ["sweden"],
    "jp": ["japan"],
    "kr": ["south korea", "korea"],
    "br": ["brazil", "brasil"],
    "il": ["israel"],
}

# Build reverse lookup: alias → canonical + all aliases
_ALIAS_GROUPS: dict[str, set[str]] = {}
for _code, _names in _COUNTRY_ALIASES.items():
    _group = {_code} | set(_names)
    for _term in _group:
        _ALIAS_GROUPS[_term] = _group


def _matches_country(text: str, preferred_locations: list[str]) -> bool:
    """Check if text matches any non-Remote preferred location or its aliases."""
    for pref in preferred_locations:
        pref_lower = pref.lower().strip()
        if pref_lower == "remote":
            continue
        if pref_lower in text:
            return True
        pref_group = _ALIAS_GROUPS.get(pref_lower)
        if pref_group and any(alias in text for alias in pref_group):
            return True
        # Reverse: check tokens in text against alias groups
        for token in text.replace(",", " ").replace("—", " ").replace("-", " ").split():
            token = token.strip()
            if token and token in _ALIAS_GROUPS and pref_lower in _ALIAS_GROUPS[token]:
                return True
    return False


def matches_location_preference(job_location: str, preferred_locations: list[str]) -> bool:
    """Check if a job's location matches the user's preferred locations.

    Returns True if:
    - No location preferences are set (accept all)
    - Job location is empty/unknown (include rather than exclude)
    - Any preferred location matches the job location (substring, alias, or "Remote")

    When both "Remote" and a country are in preferences, remote jobs must also
    match the country (or have no country specified) to be accepted.
    """
    if not preferred_locations:
        return True

    if not job_location or not job_location.strip():
        return True  # Unknown location — don't discard, let AI matcher decide

    job_lower = job_location.lower().strip()
    prefs_lower = [p.lower().strip() for p in preferred_locations]
    has_remote_pref = "remote" in prefs_lower
    has_country_prefs = any(p != "remote" for p in prefs_lower)

    # Handle remote jobs when user wants Remote + a specific country
    if "remote" in job_lower and has_remote_pref:
        if not has_country_prefs:
            return True  # Only "Remote" preference — accept any remote job

        # Strip "remote" to see if a country is mentioned
        job_rest = job_lower.replace("remote", "").strip(" ,/-|·()")
        if not job_rest:
            return False  # Just "Remote" with no country — reject (user wants a specific country)

        # Country is mentioned — check if it matches our preferences
        return _matches_country(job_rest, preferred_locations)

    # Standard location matching (non-remote jobs, or job not marked remote)
    for pref in preferred_locations:
        pref_lower = pref.lower().strip()

        if pref_lower == "remote":
            if "remote" in job_lower:
                return True
            continue

        # Direct substring match (covers "London" in "London, UK")
        if pref_lower in job_lower:
            return True

        # Check country aliases: if pref is "UK", also match "United Kingdom", "England", etc.
        pref_group = _ALIAS_GROUPS.get(pref_lower)
        if pref_group:
            if any(alias in job_lower for alias in pref_group):
                return True

        # Reverse: if job says "UK" and pref is "United Kingdom"
        for token in job_lower.replace(",", " ").replace("—", " ").replace("-", " ").split():
            token = token.strip()
            if token and token in _ALIAS_GROUPS:
                if pref_lower in _ALIAS_GROUPS[token]:
                    return True

    return False


@dataclass
class JobListing:
    """Raw job listing extracted from a platform."""
    platform: str
    external_id: str
    title: str
    company: str
    location: str
    salary_info: str | None
    description: str
    listing_url: str
    apply_url: str | None = None


class BaseScraper(ABC):
    """Abstract base class for all job scrapers."""

    platform: str = "unknown"

    def __init__(self, config: Config, db: Database, browser: BrowserManager):
        self.config = config
        self.db = db
        self.browser = browser
        self.log = get_logger()

    @abstractmethod
    async def scrape(self) -> list[JobListing]:
        """Scrape job listings and return them."""
        ...

    def filter_by_location(self, jobs: list[JobListing]) -> list[JobListing]:
        """Filter jobs by user's preferred locations. Logs discarded jobs."""
        prefs = self.config.job_preferences.locations
        if not prefs:
            return jobs

        kept = []
        discarded = 0
        for job in jobs:
            if matches_location_preference(job.location, prefs):
                kept.append(job)
            else:
                self.log.debug(
                    "[%s] Filtered out: '%s' at '%s' — location '%s' doesn't match preferences %s",
                    self.platform, job.title, job.company, job.location, prefs,
                )
                discarded += 1

        if discarded:
            self.log.info(
                "[%s] Location filter: kept %d, discarded %d (preferences: %s)",
                self.platform, len(kept), discarded, ", ".join(prefs),
            )
        return kept

    def save_jobs(self, jobs: list[JobListing]) -> int:
        """Save job listings to the database. Returns count of new jobs inserted."""
        new_count = 0
        for job in jobs:
            job_id = self.db.insert_job(
                platform=job.platform,
                external_id=job.external_id,
                title=job.title,
                company=job.company,
                location=job.location,
                salary_info=job.salary_info,
                description=job.description,
                listing_url=job.listing_url,
                apply_url=job.apply_url,
            )
            if job_id is not None:
                new_count += 1
        self.log.info("[%s] Saved %d new jobs (of %d scraped)", self.platform, new_count, len(jobs))
        return new_count

    def _build_search_queries(self) -> list[dict]:
        """Build search query combinations from config preferences."""
        queries = []
        for title in self.config.job_preferences.titles:
            for location in self.config.job_preferences.locations:
                queries.append({"title": title, "location": location})
        return queries
