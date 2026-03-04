"""JobSpy multi-board scraper — wraps python-jobspy for 5+ platforms."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

import pandas as pd

from src.config import Config
from src.database import Database
from src.scrapers.base import JobListing, matches_location_preference
from src.utils.logging import get_logger


class JobSpyScraper:
    """Scrapes jobs from LinkedIn, Indeed, Glassdoor, ZipRecruiter, and Google via jobspy."""

    SUPPORTED_PLATFORMS = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google"]
    # ZipRecruiter is geoblocked in EU/UK/EEA due to GDPR
    GDPR_BLOCKED_PLATFORMS = {"zip_recruiter"}
    GDPR_REGIONS = {
        "uk", "united kingdom", "gb", "great britain", "england", "scotland",
        "wales", "northern ireland", "ireland", "germany", "france", "spain",
        "italy", "netherlands", "belgium", "austria", "sweden", "denmark",
        "finland", "norway", "poland", "portugal", "czech republic", "romania",
        "hungary", "greece", "switzerland", "eu", "europe",
    }

    # Normalize shorthand locations → platform-friendly format.
    # Keys are lowercase; values are (search_location, country_indeed_code).
    # Platforms like LinkedIn/Indeed/Google resolve full country names fine.
    # Glassdoor may fail on country-level strings — that's handled gracefully
    # (it just returns no Glassdoor results for that query).
    LOCATION_ALIASES: dict[str, tuple[str, str]] = {
        "uk":               ("United Kingdom", "UK"),
        "gb":               ("United Kingdom", "UK"),
        "great britain":    ("United Kingdom", "UK"),
        "england":          ("England, United Kingdom", "UK"),
        "scotland":         ("Scotland, United Kingdom", "UK"),
        "wales":            ("Wales, United Kingdom", "UK"),
        "northern ireland": ("Northern Ireland, United Kingdom", "UK"),
        "us":               ("United States", "USA"),
        "usa":              ("United States", "USA"),
        "united states":    ("United States", "USA"),
        "canada":           ("Canada", "Canada"),
        "ca":               ("Canada", "Canada"),
        "germany":          ("Germany", "Germany"),
        "de":               ("Germany", "Germany"),
        "france":           ("France", "France"),
        "fr":               ("France", "France"),
        "netherlands":      ("Netherlands", "Netherlands"),
        "nl":               ("Netherlands", "Netherlands"),
        "ireland":          ("Ireland", "Ireland"),
        "spain":            ("Spain", "Spain"),
        "italy":            ("Italy", "Italy"),
        "australia":        ("Australia", "Australia"),
        "au":               ("Australia", "Australia"),
        "india":            ("India", "India"),
        "in":               ("India", "India"),
        "singapore":        ("Singapore", "Singapore"),
        "sg":               ("Singapore", "Singapore"),
        "sweden":           ("Sweden", "Sweden"),
        "switzerland":      ("Switzerland", "Switzerland"),
        "poland":           ("Poland", "Poland"),
        "portugal":         ("Portugal", "Portugal"),
        "belgium":          ("Belgium", "Belgium"),
        "austria":          ("Austria", "Austria"),
        "denmark":          ("Denmark", "Denmark"),
        "finland":          ("Finland", "Finland"),
        "norway":           ("Norway", "Norway"),
    }

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.log = get_logger()
        self.platform = "jobspy"

    async def scrape(self, platforms: list[str] | None = None) -> list[JobListing]:
        """Run jobspy across specified platforms (or all supported)."""
        site_names = platforms or self.SUPPORTED_PLATFORMS
        # Validate platform names
        site_names = [s for s in site_names if s in self.SUPPORTED_PLATFORMS]
        if not site_names:
            self.log.warning("[jobspy] No valid platforms specified.")
            return []

        all_jobs: list[JobListing] = []
        queries = self._build_search_queries()

        for query in queries:
            # Exclude GDPR-blocked platforms for EU/UK locations
            gdpr_location = query.get("country_indeed") or query["location"]
            active_sites = self._filter_platforms_for_location(site_names, gdpr_location)
            if not active_sites:
                self.log.warning("[jobspy] No valid platforms for location '%s'", query["location"])
                continue

            self.log.info(
                "[jobspy] Searching: '%s' in '%s' on %s",
                query["title"], query["location"], ", ".join(active_sites),
            )
            try:
                df = await asyncio.to_thread(
                    self._run_jobspy,
                    search_term=query["title"],
                    location=query["location"],
                    site_names=active_sites,
                    country_indeed=query.get("country_indeed"),
                )
                if df is not None and not df.empty:
                    jobs = self._dataframe_to_jobs(df)
                    all_jobs.extend(jobs)
                    self.log.info("[jobspy] Found %d jobs for '%s'", len(jobs), query["title"])
                else:
                    self.log.info("[jobspy] No results for '%s' in '%s'", query["title"], query["location"])
            except Exception as e:
                self.log.error("[jobspy] Search failed for '%s': %s", query["title"], e)

        # Post-filter by location preference (upstream APIs may return non-matching results)
        loc_prefs = self.config.job_preferences.locations
        if loc_prefs:
            before = len(all_jobs)
            all_jobs = [j for j in all_jobs if matches_location_preference(j.location, loc_prefs)]
            filtered = before - len(all_jobs)
            if filtered:
                self.log.info("[jobspy] Location filter: kept %d, discarded %d (preferences: %s)",
                              len(all_jobs), filtered, ", ".join(loc_prefs))

        # Post-filter by minimum salary
        min_salary = self.config.job_preferences.min_salary
        if min_salary:
            before = len(all_jobs)
            all_jobs = [j for j in all_jobs if self._meets_salary_threshold(j.salary_info, min_salary)]
            filtered = before - len(all_jobs)
            if filtered:
                self.log.info("[jobspy] Salary filter (>=%s): kept %d, discarded %d",
                              f"{min_salary:,}", len(all_jobs), filtered)

        # Save to DB
        new_count = self._save_jobs(all_jobs)
        self.log.info("[jobspy] Total: %d jobs found, %d new saved", len(all_jobs), new_count)
        return all_jobs

    def _run_jobspy(
        self, search_term: str, location: str, site_names: list[str],
        country_indeed: str | None = None,
    ) -> pd.DataFrame | None:
        """Synchronous jobspy call — run via to_thread."""
        from jobspy import scrape_jobs

        prefs = self.config.job_preferences
        kwargs = dict(
            site_name=site_names,
            search_term=search_term,
            location=location,
            results_wanted=prefs.results_wanted,
            hours_old=prefs.hours_old,
            country_indeed=country_indeed or prefs.country_indeed or "UK",
            is_remote=prefs.is_remote,
            distance=prefs.distance or 50,
            enforce_annual_salary=True,
        )
        if prefs.job_type:
            kwargs["job_type"] = prefs.job_type

        try:
            df = scrape_jobs(**kwargs)
            return df
        except Exception as e:
            self.log.error("[jobspy] scrape_jobs error: %s", e)
            return None

    def _dataframe_to_jobs(self, df: pd.DataFrame) -> list[JobListing]:
        """Convert a jobspy DataFrame to JobListing objects."""
        jobs = []
        for _, row in df.iterrows():
            try:
                site = str(row.get("site", "unknown"))
                job_url = str(row.get("job_url", ""))

                # Build external ID as md5 hash of site:job_url (truncated to 16 chars)
                raw_id = f"{site}:{job_url}"
                external_id = hashlib.md5(raw_id.encode()).hexdigest()[:16]

                # Build location string
                parts = []
                for col in ("city", "state", "country"):
                    val = row.get(col)
                    if pd.notna(val) and str(val).strip():
                        parts.append(str(val).strip())
                is_remote = row.get("is_remote")
                if is_remote is True or str(is_remote).lower() == "true":
                    parts.append("Remote")
                location = ", ".join(parts) if parts else ""

                # Build salary info
                salary_info = self._build_salary(row)

                title = str(row.get("title", "")).strip()
                company = str(row.get("company_name", row.get("company", ""))).strip()
                description = str(row.get("description", "")).strip()

                if not title or not job_url:
                    continue

                jobs.append(JobListing(
                    platform=site,
                    external_id=external_id,
                    title=title,
                    company=company,
                    location=location,
                    salary_info=salary_info,
                    description=description,
                    listing_url=job_url,
                    apply_url=job_url,
                ))
            except Exception as e:
                self.log.debug("[jobspy] Skipping row: %s", e)
                continue
        return jobs

    def _build_salary(self, row) -> str | None:
        """Build salary string from jobspy columns."""
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        currency = row.get("currency", "")
        interval = row.get("interval", "")

        if pd.isna(min_amt) and pd.isna(max_amt):
            return None

        parts = []
        if pd.notna(currency):
            parts.append(str(currency))
        if pd.notna(min_amt):
            parts.append(f"{float(min_amt):,.0f}")
        if pd.notna(max_amt):
            parts.append(f"- {float(max_amt):,.0f}")
        if pd.notna(interval) and str(interval).strip():
            parts.append(f"/{interval}")

        return " ".join(parts) if parts else None

    @staticmethod
    def _meets_salary_threshold(salary_info: str | None, min_salary: int) -> bool:
        """Check if a job's salary meets the minimum threshold.

        Jobs with no salary info are kept (benefit of the doubt).
        If any number in the salary string meets or exceeds the threshold, the job passes.
        This covers both min_amount and max_amount (i.e. estimated ranges).
        """
        if not salary_info:
            return True
        import re
        numbers = re.findall(r"[\d,]+(?:\.\d+)?", salary_info)
        if not numbers:
            return True
        # Check if any salary figure meets the threshold
        return any(float(n.replace(",", "")) >= min_salary for n in numbers)

    def _save_jobs(self, jobs: list[JobListing]) -> int:
        """Save jobs to DB, deduplicating via UNIQUE constraint."""
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
        return new_count

    def _filter_platforms_for_location(self, platforms: list[str], location: str) -> list[str]:
        """Remove GDPR-blocked platforms when searching in EU/UK regions."""
        loc_lower = location.lower().strip()
        is_gdpr = any(region in loc_lower for region in self.GDPR_REGIONS)
        if not is_gdpr:
            return platforms
        filtered = [p for p in platforms if p not in self.GDPR_BLOCKED_PLATFORMS]
        removed = set(platforms) - set(filtered)
        if removed:
            self.log.info("[jobspy] Excluded %s for GDPR region '%s'", ", ".join(removed), location)
        return filtered

    def _normalize_location(self, location: str) -> tuple[str, str | None]:
        """Normalize a config location to (search_location, country_indeed).

        Returns the platform-friendly search string and an optional
        country_indeed override derived from the alias map.
        """
        key = location.lower().strip()
        if key in self.LOCATION_ALIASES:
            search_loc, country_code = self.LOCATION_ALIASES[key]
            self.log.debug("[jobspy] Normalized location '%s' → '%s'", location, search_loc)
            return search_loc, country_code
        return location, None

    def _build_search_queries(self) -> list[dict]:
        """Build title x location search combinations.

        Shorthand locations (e.g. "UK") are normalized to full names
        (e.g. "United Kingdom") via LOCATION_ALIASES.

        When locations include both "Remote" and a country (e.g. ["Remote", "UK"]),
        skip "Remote" as a standalone search location — it would search globally and
        flood results with jobs from other countries.  The is_remote API flag already
        filters for remote jobs within the country-specific searches.
        """
        raw_locations = list(self.config.job_preferences.locations)
        country_locations = [loc for loc in raw_locations if loc.lower().strip() != "remote"]

        # If we have both "Remote" and real countries, drop the bare "Remote" search
        if country_locations and len(country_locations) < len(raw_locations):
            self.log.info(
                "[jobspy] Skipping global 'Remote' search — will use is_remote flag "
                "with country searches: %s", ", ".join(country_locations),
            )
            raw_locations = country_locations

        queries = []
        for title in self.config.job_preferences.titles:
            for loc in raw_locations:
                search_loc, country_code = self._normalize_location(loc)
                queries.append({
                    "title": title,
                    "location": search_loc,
                    "country_indeed": country_code,
                })
        return queries
