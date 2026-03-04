from __future__ import annotations

import asyncio

import httpx

from src.scrapers.base import BaseScraper, JobListing


# Map user-facing location names to Adzuna country codes
_LOCATION_TO_COUNTRY: dict[str, str] = {
    "uk": "gb", "united kingdom": "gb", "england": "gb", "scotland": "gb",
    "wales": "gb", "britain": "gb", "gb": "gb", "great britain": "gb",
    "us": "us", "usa": "us", "united states": "us", "america": "us",
    "germany": "de", "deutschland": "de", "de": "de",
    "france": "fr", "fr": "fr",
    "australia": "au", "au": "au",
    "new zealand": "nz", "nz": "nz",
    "canada": "ca", "ca": "ca",
    "india": "in", "in": "in",
    "poland": "pl", "pl": "pl",
    "brazil": "br", "brasil": "br", "br": "br",
    "austria": "at", "at": "at",
    "south africa": "za", "za": "za",
    "remote": "gb",  # Default to GB for remote searches
}

DEFAULT_COUNTRY = "gb"


class AdzunaScraper(BaseScraper):
    """Scrape job listings via the Adzuna REST API."""

    platform = "adzuna"

    API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
    RESULTS_PER_PAGE = 50  # Adzuna max

    async def scrape(self) -> list[JobListing]:
        app_id = self.config.adzuna.app_id
        app_key = self.config.adzuna.app_key

        if not app_id or not app_key:
            self.log.warning("[adzuna] No API credentials configured — skipping. "
                             "Sign up free at https://developer.adzuna.com/")
            return []

        all_jobs: list[JobListing] = []
        queries = self._build_search_queries()
        hours_old = getattr(self.config.job_preferences, "hours_old", 72)
        max_days = max(1, hours_old // 24)

        async with httpx.AsyncClient(timeout=30) as client:
            for query in queries:
                title, location = query["title"], query["location"]
                country = _LOCATION_TO_COUNTRY.get(location.lower().strip(), DEFAULT_COUNTRY)

                self.log.info("[adzuna] Searching: %s in %s (country=%s)", title, location, country)
                run_id = self.db.start_search_run("adzuna", f"{title} - {location}")

                try:
                    jobs = await self._search_api(client, title, location, country,
                                                  app_id, app_key, max_days)
                    all_jobs.extend(jobs)
                    self.db.finish_search_run(run_id, jobs_found=len(jobs))
                    self.log.info("[adzuna] Found %d jobs for '%s'", len(jobs), title)
                except Exception as e:
                    self.log.error("[adzuna] Search failed for '%s': %s", title, e)
                    self.db.finish_search_run(run_id, jobs_found=0)

                await asyncio.sleep(2)

        all_jobs = self.filter_by_location(all_jobs)
        new_count = self.save_jobs(all_jobs)
        self.log.info("[adzuna] Total: %d jobs after location filter, %d new", len(all_jobs), new_count)
        return all_jobs

    async def _search_api(
        self, client: httpx.AsyncClient, title: str, location: str,
        country: str, app_id: str, app_key: str, max_days: int,
    ) -> list[JobListing]:
        """Fetch up to 2 pages (100 results) from Adzuna for a single query."""
        jobs: list[JobListing] = []
        results_wanted = getattr(self.config.job_preferences, "results_wanted", 50)
        max_pages = max(1, (results_wanted + self.RESULTS_PER_PAGE - 1) // self.RESULTS_PER_PAGE)
        max_pages = min(max_pages, 4)  # Cap to avoid rate limits

        where = location if location.lower() != "remote" else ""

        for page in range(1, max_pages + 1):
            url = self.API_URL.format(country=country, page=page)
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "what": title,
                "results_per_page": self.RESULTS_PER_PAGE,
                "max_days_old": max_days,
                "sort_by": "date",
                "content-type": "application/json",
            }
            if where:
                params["where"] = where

            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            if not results:
                break

            for item in results:
                job = self._parse_result(item)
                if job:
                    jobs.append(job)

            # Stop if we got fewer than a full page
            if len(results) < self.RESULTS_PER_PAGE:
                break

            await asyncio.sleep(2)

        # Resolve Adzuna redirect URLs to final destination URLs.
        # Adzuna returns tracking redirect_urls that can cause Playwright
        # navigation timeouts (60s+). Resolving them here avoids wasted
        # turns during the application phase.
        if jobs:
            await self._resolve_redirect_urls(client, jobs)

        return jobs

    async def _resolve_redirect_urls(
        self, client: httpx.AsyncClient, jobs: list[JobListing],
    ) -> None:
        """Follow Adzuna redirect URLs to get final destination URLs."""
        sem = asyncio.Semaphore(5)  # limit concurrent requests

        async def _resolve_one(job: JobListing) -> None:
            async with sem:
                try:
                    # HEAD request with redirects to get final URL
                    resp = await client.head(
                        job.apply_url,
                        follow_redirects=True,
                        timeout=15,
                    )
                    final_url = str(resp.url)
                    if final_url and final_url != job.apply_url:
                        self.log.debug(
                            "[adzuna] Resolved %s -> %s",
                            job.apply_url[:80], final_url[:80],
                        )
                        job.apply_url = final_url
                        job.listing_url = final_url
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    # Keep the original redirect URL if resolution fails
                    self.log.debug("[adzuna] Could not resolve %s: %s", job.apply_url[:80], e)

        await asyncio.gather(*[_resolve_one(j) for j in jobs])

    def _parse_result(self, item: dict) -> JobListing | None:
        """Parse a single Adzuna API result into a JobListing."""
        external_id = str(item.get("id", ""))
        if not external_id:
            return None

        title = item.get("title", "").strip()
        if not title:
            return None

        company_data = item.get("company", {})
        company = company_data.get("display_name", "Unknown") if isinstance(company_data, dict) else "Unknown"

        location_data = item.get("location", {})
        location = location_data.get("display_name", "") if isinstance(location_data, dict) else ""

        redirect_url = item.get("redirect_url", "")
        if not redirect_url:
            return None

        salary_info = self._format_salary(item)
        description = item.get("description", "")

        return JobListing(
            platform="adzuna",
            external_id=external_id,
            title=title,
            company=company,
            location=location,
            salary_info=salary_info,
            description=description[:5000],
            listing_url=redirect_url,
            apply_url=redirect_url,
        )

    @staticmethod
    def _format_salary(item: dict) -> str | None:
        """Format salary fields into a readable string."""
        sal_min = item.get("salary_min")
        sal_max = item.get("salary_max")

        if not sal_min and not sal_max:
            return None

        predicted = item.get("salary_is_predicted") == "1"
        prefix = "~" if predicted else ""

        if sal_min and sal_max:
            if sal_min == sal_max:
                return f"{prefix}\u00a3{sal_min:,.0f}/yr"
            return f"{prefix}\u00a3{sal_min:,.0f} - \u00a3{sal_max:,.0f}/yr"
        elif sal_min:
            return f"{prefix}\u00a3{sal_min:,.0f}+/yr"
        else:
            return f"Up to {prefix}\u00a3{sal_max:,.0f}/yr"
