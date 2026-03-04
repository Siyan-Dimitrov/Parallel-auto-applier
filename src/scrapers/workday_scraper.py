"""Workday Direct Scraper — uses Workday's public JSON API (no browser)."""
from __future__ import annotations

import asyncio
import hashlib

import httpx

from src.config import Config, WorkdayEmployer
from src.database import Database
from src.scrapers.base import JobListing, matches_location_preference
from src.utils.logging import get_logger


class WorkdayDirectScraper:
    """Pure HTTP scraper for Workday career portals via their public JSON API."""

    MAX_RESULTS_PER_EMPLOYER = 100
    PAGE_SIZE = 20

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.log = get_logger()
        self.platform = "workday_direct"

    async def scrape(self) -> list[JobListing]:
        """Scrape all configured Workday employers."""
        employers = self.config.employers.workday_employers
        if not employers:
            self.log.info("[workday_direct] No employers configured.")
            return []

        all_jobs: list[JobListing] = []
        queries = self._build_search_queries()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for employer in employers:
                self.log.info("[workday_direct] Scraping: %s", employer.name)
                for query in queries:
                    try:
                        jobs = await self._scrape_employer(
                            client, employer, query["title"],
                        )
                        all_jobs.extend(jobs)
                    except Exception as e:
                        self.log.error(
                            "[workday_direct] Failed for %s / '%s': %s",
                            employer.name, query["title"], e,
                        )
                # 1s delay between employers
                await asyncio.sleep(1.0)

        # Post-filter by location preference
        prefs = self.config.job_preferences.locations
        if prefs:
            before = len(all_jobs)
            all_jobs = [j for j in all_jobs if matches_location_preference(j.location, prefs)]
            filtered = before - len(all_jobs)
            if filtered:
                self.log.info("[workday_direct] Location filter: kept %d, discarded %d (preferences: %s)",
                              len(all_jobs), filtered, ", ".join(prefs))

        new_count = self._save_jobs(all_jobs)
        self.log.info("[workday_direct] Total: %d jobs found, %d new saved", len(all_jobs), new_count)
        return all_jobs

    async def _scrape_employer(
        self,
        client: httpx.AsyncClient,
        employer: WorkdayEmployer,
        search_text: str,
    ) -> list[JobListing]:
        """Paginate through a single employer's job listings."""
        jobs: list[JobListing] = []
        offset = 0

        while offset < self.MAX_RESULTS_PER_EMPLOYER:
            url = f"{employer.base_url}/wday/cxs/{employer.tenant}/{employer.site_id}/jobs"
            payload = {
                "searchText": search_text,
                "limit": self.PAGE_SIZE,
                "offset": offset,
            }

            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                self.log.error("[workday_direct] API error for %s: %s", employer.name, e)
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break

            total = data.get("total", 0)

            for posting in postings:
                job = self._parse_posting(posting, employer)
                if job:
                    jobs.append(job)

            offset += self.PAGE_SIZE

            # Stop if we've retrieved all available
            if offset >= total:
                break

            # 0.5s delay between pages
            await asyncio.sleep(0.5)

        self.log.info(
            "[workday_direct] %s / '%s': found %d jobs",
            employer.name, search_text, len(jobs),
        )
        return jobs

    def _parse_posting(self, posting: dict, employer: WorkdayEmployer) -> JobListing | None:
        """Parse a single Workday job posting into a JobListing."""
        try:
            title = posting.get("title", "").strip()
            if not title:
                return None

            # Build external ID
            external_path = posting.get("externalPath", "")
            raw_id = f"workday:{employer.tenant}:{external_path}"
            external_id = hashlib.md5(raw_id.encode()).hexdigest()[:16]

            # Build listing URL
            listing_url = f"{employer.base_url}/en-US{external_path}" if external_path else employer.base_url

            # Location
            locales = posting.get("locationsText", "")

            # Posted date
            posted = posting.get("postedOn", "")

            # Build description from bullet points if available
            bullet_fields = posting.get("bulletFields", [])
            description_parts = [title]
            if posted:
                description_parts.append(f"Posted: {posted}")
            for bf in bullet_fields:
                description_parts.append(str(bf))

            return JobListing(
                platform="workday_direct",
                external_id=external_id,
                title=title,
                company=employer.name,
                location=locales,
                salary_info=None,
                description="\n".join(description_parts),
                listing_url=listing_url,
                apply_url=listing_url,
            )
        except Exception as e:
            self.log.debug("[workday_direct] Skipping posting: %s", e)
            return None

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

    def _build_search_queries(self) -> list[dict]:
        """Build search queries from config preferences."""
        queries = []
        for title in self.config.job_preferences.titles:
            queries.append({"title": title})
        return queries
