from __future__ import annotations

import asyncio
import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from src.scrapers.base import BaseScraper, JobListing


class LinkedInScraper(BaseScraper):
    """Scrape public LinkedIn job listings (no login required)."""

    platform = "linkedin"

    # LinkedIn public job search URL pattern
    BASE_URL = "https://www.linkedin.com/jobs/search"

    async def scrape(self) -> list[JobListing]:
        all_jobs: list[JobListing] = []
        queries = self._build_search_queries()

        async with self.browser.new_page() as page:
            for query in queries:
                self.log.info("[linkedin] Searching: %s in %s", query["title"], query["location"])
                run_id = self.db.start_search_run("linkedin", f"{query['title']} - {query['location']}")

                try:
                    jobs = await self._scrape_query(page, query["title"], query["location"])
                    all_jobs.extend(jobs)
                    self.db.finish_search_run(run_id, jobs_found=len(jobs))
                except Exception as e:
                    self.log.error("[linkedin] Search failed for '%s': %s", query["title"], e)
                    self.db.finish_search_run(run_id, jobs_found=0)

                # Delay between queries
                await asyncio.sleep(3)

        new_count = self.save_jobs(all_jobs)
        self.log.info("[linkedin] Total: %d jobs scraped, %d new", len(all_jobs), new_count)
        return all_jobs

    async def _scrape_query(self, page, title: str, location: str) -> list[JobListing]:
        url = (
            f"{self.BASE_URL}?"
            f"keywords={quote_plus(title)}"
            f"&location={quote_plus(location)}"
            f"&trk=public_jobs_jobs-search-bar_search-submit"
            f"&position=1&pageNum=0"
        )

        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Scroll to load more jobs
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)

        # Try clicking "See more jobs" button if it exists
        try:
            see_more = page.locator("button.infinite-scroller__show-more-button")
            if await see_more.is_visible(timeout=2000):
                await see_more.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        content = await page.content()
        return self._parse_listings(content)

    def _parse_listings(self, html: str) -> list[JobListing]:
        soup = BeautifulSoup(html, "lxml")
        jobs: list[JobListing] = []

        # LinkedIn public job cards
        cards = soup.select("div.base-card, li.jobs-search-results__list-item, div.job-search-card")

        for card in cards:
            try:
                # Title
                title_el = card.select_one(
                    "h3.base-search-card__title, "
                    "a.base-card__full-link, "
                    "h3.job-search-card__title"
                )
                title = title_el.get_text(strip=True) if title_el else None
                if not title:
                    continue

                # Company
                company_el = card.select_one(
                    "h4.base-search-card__subtitle, "
                    "a.hidden-nested-link, "
                    "h4.job-search-card__subtitle"
                )
                company = company_el.get_text(strip=True) if company_el else "Unknown"

                # Location
                location_el = card.select_one(
                    "span.job-search-card__location, "
                    "span.base-search-card__metadata"
                )
                location = location_el.get_text(strip=True) if location_el else ""

                # Link
                link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
                listing_url = link_el["href"] if link_el and link_el.has_attr("href") else None
                if not listing_url:
                    continue

                # Extract job ID from URL
                match = re.search(r"/jobs/view/(\d+)", listing_url)
                external_id = match.group(1) if match else listing_url

                jobs.append(JobListing(
                    platform="linkedin",
                    external_id=external_id,
                    title=title,
                    company=company,
                    location=location,
                    salary_info=None,
                    description="",  # Will be fetched if needed
                    listing_url=listing_url.split("?")[0],
                ))
            except Exception as e:
                self.log.debug("[linkedin] Failed to parse card: %s", e)
                continue

        return jobs
