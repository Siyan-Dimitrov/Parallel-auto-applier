from __future__ import annotations

import asyncio
import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from src.scrapers.base import BaseScraper, JobListing


class IndeedScraper(BaseScraper):
    """Scrape public Indeed job listings (no login required)."""

    platform = "indeed"

    BASE_URL = "https://www.indeed.com/jobs"

    async def scrape(self) -> list[JobListing]:
        all_jobs: list[JobListing] = []
        queries = self._build_search_queries()

        async with self.browser.new_page() as page:
            for query in queries:
                self.log.info("[indeed] Searching: %s in %s", query["title"], query["location"])
                run_id = self.db.start_search_run("indeed", f"{query['title']} - {query['location']}")

                try:
                    jobs = await self._scrape_query(page, query["title"], query["location"])
                    all_jobs.extend(jobs)
                    self.db.finish_search_run(run_id, jobs_found=len(jobs))
                except Exception as e:
                    self.log.error("[indeed] Search failed for '%s': %s", query["title"], e)
                    self.db.finish_search_run(run_id, jobs_found=0)

                await asyncio.sleep(3)

        new_count = self.save_jobs(all_jobs)
        self.log.info("[indeed] Total: %d jobs scraped, %d new", len(all_jobs), new_count)
        return all_jobs

    async def _scrape_query(self, page, title: str, location: str) -> list[JobListing]:
        all_page_jobs: list[JobListing] = []

        # Scrape first 3 pages
        for page_num in range(3):
            start = page_num * 10
            url = (
                f"{self.BASE_URL}?"
                f"q={quote_plus(title)}"
                f"&l={quote_plus(location)}"
                f"&start={start}"
            )

            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            content = await page.content()
            jobs = self._parse_listings(content)

            if not jobs:
                break

            all_page_jobs.extend(jobs)
            self.log.debug("[indeed] Page %d: found %d jobs", page_num + 1, len(jobs))
            await asyncio.sleep(2)

        return all_page_jobs

    def _parse_listings(self, html: str) -> list[JobListing]:
        soup = BeautifulSoup(html, "lxml")
        jobs: list[JobListing] = []

        # Indeed job cards
        cards = soup.select(
            "div.job_seen_beacon, "
            "div.jobsearch-ResultsList > div, "
            "td.resultContent, "
            "div.cardOutline"
        )

        for card in cards:
            try:
                # Title
                title_el = card.select_one(
                    "h2.jobTitle a, "
                    "a.jcs-JobTitle, "
                    "h2.jobTitle span"
                )
                title = title_el.get_text(strip=True) if title_el else None
                if not title:
                    continue

                # Company
                company_el = card.select_one(
                    "span.companyName, "
                    "span[data-testid='company-name'], "
                    "div.companyInfo span"
                )
                company = company_el.get_text(strip=True) if company_el else "Unknown"

                # Location
                location_el = card.select_one(
                    "div.companyLocation, "
                    "span.companyLocation, "
                    "div[data-testid='text-location']"
                )
                location = location_el.get_text(strip=True) if location_el else ""

                # Salary
                salary_el = card.select_one(
                    "div.salary-snippet-container, "
                    "span.estimated-salary, "
                    "div.metadata.salary-snippet-container"
                )
                salary = salary_el.get_text(strip=True) if salary_el else None

                # Link + job ID
                link_el = card.select_one("a[href*='clk'], a.jcs-JobTitle, h2.jobTitle a")
                if link_el and link_el.has_attr("href"):
                    href = link_el["href"]
                    listing_url = f"https://www.indeed.com{href}" if href.startswith("/") else href
                else:
                    continue

                jk_match = re.search(r"jk=([a-f0-9]+)", listing_url)
                external_id = jk_match.group(1) if jk_match else listing_url

                # Description snippet
                desc_el = card.select_one("div.job-snippet, td.snip")
                description = desc_el.get_text(strip=True) if desc_el else ""

                jobs.append(JobListing(
                    platform="indeed",
                    external_id=external_id,
                    title=title,
                    company=company,
                    location=location,
                    salary_info=salary,
                    description=description,
                    listing_url=listing_url,
                ))
            except Exception as e:
                self.log.debug("[indeed] Failed to parse card: %s", e)
                continue

        return jobs
