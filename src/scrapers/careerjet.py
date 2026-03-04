from __future__ import annotations

import asyncio
import hashlib

import httpx

from src.scrapers.base import BaseScraper, JobListing


# Map user-facing location names to Careerjet locale codes
_LOCATION_TO_LOCALE: dict[str, str] = {
    "uk": "en_GB", "united kingdom": "en_GB", "england": "en_GB",
    "scotland": "en_GB", "wales": "en_GB", "britain": "en_GB",
    "gb": "en_GB", "great britain": "en_GB",
    "us": "en_US", "usa": "en_US", "united states": "en_US", "america": "en_US",
    "germany": "de_DE", "deutschland": "de_DE", "de": "de_DE",
    "france": "fr_FR", "fr": "fr_FR",
    "australia": "en_AU", "au": "en_AU",
    "canada": "en_CA", "ca": "en_CA",
    "india": "en_IN", "in": "en_IN",
    "netherlands": "nl_NL", "holland": "nl_NL", "nl": "nl_NL",
    "ireland": "en_IE", "ie": "en_IE",
    "switzerland": "de_CH", "ch": "de_CH",
    "sweden": "sv_SE", "se": "sv_SE",
    "brazil": "pt_BR", "brasil": "pt_BR", "br": "pt_BR",
    "remote": "en_GB",  # Default to GB for remote searches
}

DEFAULT_LOCALE = "en_GB"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class CareerjetScraper(BaseScraper):
    """Scrape job listings via the Careerjet REST API."""

    platform = "careerjet"

    API_URL = "https://search.api.careerjet.net/v4/query"
    PAGE_SIZE = 99  # Careerjet max is 100

    async def scrape(self) -> list[JobListing]:
        affid = self.config.careerjet.affid

        if not affid:
            self.log.warning("[careerjet] No affiliate ID configured — skipping. "
                             "Sign up free at https://www.careerjet.com/partners/api")
            return []

        all_jobs: list[JobListing] = []
        queries = self._build_search_queries()

        async with httpx.AsyncClient(timeout=30) as client:
            for query in queries:
                title, location = query["title"], query["location"]
                locale = _LOCATION_TO_LOCALE.get(location.lower().strip(), DEFAULT_LOCALE)

                self.log.info("[careerjet] Searching: %s in %s (locale=%s)", title, location, locale)
                run_id = self.db.start_search_run("careerjet", f"{title} - {location}")

                try:
                    jobs = await self._search_api(client, title, location, locale, affid)
                    all_jobs.extend(jobs)
                    self.db.finish_search_run(run_id, jobs_found=len(jobs))
                    self.log.info("[careerjet] Found %d jobs for '%s'", len(jobs), title)
                except Exception as e:
                    self.log.error("[careerjet] Search failed for '%s': %s", title, e)
                    self.db.finish_search_run(run_id, jobs_found=0)

                await asyncio.sleep(2)

        all_jobs = self.filter_by_location(all_jobs)
        new_count = self.save_jobs(all_jobs)
        self.log.info("[careerjet] Total: %d jobs after location filter, %d new", len(all_jobs), new_count)
        return all_jobs

    async def _search_api(
        self, client: httpx.AsyncClient, title: str, location: str,
        locale: str, affid: str,
    ) -> list[JobListing]:
        """Fetch up to 2 pages from Careerjet for a single query."""
        jobs: list[JobListing] = []
        results_wanted = getattr(self.config.job_preferences, "results_wanted", 50)
        max_pages = max(1, (results_wanted + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        max_pages = min(max_pages, 3)  # Cap pages

        where = location if location.lower() != "remote" else ""

        for page in range(1, max_pages + 1):
            params = {
                "locale_code": locale,
                "keywords": title,
                "pagesize": self.PAGE_SIZE,
                "page": page,
                "sort": "date",
                "affid": affid,
                "user_ip": "1.0.0.1",
                "user_agent": DEFAULT_USER_AGENT,
                "url": "https://jobbot.local/search",
            }
            if where:
                params["location"] = where

            resp = await client.get(self.API_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            result_jobs = data.get("jobs", [])
            if not result_jobs:
                break

            for item in result_jobs:
                job = self._parse_result(item)
                if job:
                    jobs.append(job)

            # Stop if we got fewer than a full page
            if len(result_jobs) < self.PAGE_SIZE:
                break

            await asyncio.sleep(2)

        return jobs

    def _parse_result(self, item: dict) -> JobListing | None:
        """Parse a single Careerjet API result into a JobListing."""
        title = item.get("title", "").strip()
        if not title:
            return None

        url = item.get("url", "")
        if not url:
            return None

        # Careerjet doesn't always provide a unique ID — hash the URL
        external_id = hashlib.md5(url.encode()).hexdigest()[:16]

        company = item.get("company", "Unknown") or "Unknown"
        location = item.get("locations", "") or ""
        description = item.get("description", "")
        salary_info = self._format_salary(item)

        return JobListing(
            platform="careerjet",
            external_id=external_id,
            title=title,
            company=company,
            location=location,
            salary_info=salary_info,
            description=description[:5000],
            listing_url=url,
            apply_url=url,
        )

    @staticmethod
    def _format_salary(item: dict) -> str | None:
        """Format salary fields into a readable string."""
        # Careerjet provides a pre-formatted salary string
        salary_str = item.get("salary", "")
        if salary_str:
            return salary_str

        sal_min = item.get("salary_min")
        sal_max = item.get("salary_max")
        currency = item.get("salary_currency_code", "")

        if not sal_min and not sal_max:
            return None

        symbol = currency or "\u00a3"
        if sal_min and sal_max:
            return f"{symbol}{sal_min:,.0f} - {symbol}{sal_max:,.0f}"
        elif sal_min:
            return f"{symbol}{sal_min:,.0f}+"
        else:
            return f"Up to {symbol}{sal_max:,.0f}"
