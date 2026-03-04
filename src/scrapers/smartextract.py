"""SmartExtract — AI-driven career page scraper using Playwright + Ollama."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import ollama as ollama_client
from bs4 import BeautifulSoup

from src.browser import BrowserManager
from src.config import Config, CareerSite
from src.database import Database
from src.scrapers.base import JobListing
from src.utils.logging import get_logger

MAX_HTML_SIZE = 8192  # 8KB cap for HTML sent to LLM


class SmartExtractScraper:
    """Two-phase AI-driven career page scraper.

    Phase 1 (Intelligence): Playwright loads page, intercepts API responses,
    extracts JSON-LD, captures cleaned HTML.

    Phase 2 (Strategy): Ollama analyzes the intelligence and selects an
    extraction strategy (json_ld, api_response, or css_selectors).
    """

    def __init__(self, config: Config, db: Database, browser: BrowserManager):
        self.config = config
        self.db = db
        self.browser = browser
        self.log = get_logger()
        self.platform = "smartextract"
        self.ollama = ollama_client.Client(host=config.ollama.base_url)
        self.model = config.ollama.model

    async def scrape(self) -> list[JobListing]:
        """Scrape all configured career pages."""
        pages = self.config.sites.career_pages
        if not pages:
            self.log.info("[smartextract] No career pages configured.")
            return []

        all_jobs: list[JobListing] = []

        for site in pages:
            self.log.info("[smartextract] Processing: %s (%s)", site.name, site.url)
            try:
                jobs = await self._scrape_site(site)
                all_jobs.extend(jobs)
                self.log.info("[smartextract] %s: found %d jobs", site.name, len(jobs))
            except Exception as e:
                self.log.error("[smartextract] Failed for %s: %s", site.name, e)

        new_count = self._save_jobs(all_jobs)
        self.log.info("[smartextract] Total: %d jobs found, %d new saved", len(all_jobs), new_count)
        return all_jobs

    async def _scrape_site(self, site: CareerSite) -> list[JobListing]:
        """Scrape a single career page using the two-phase approach."""
        # Phase 1: Intelligence collection
        intelligence = await self._collect_intelligence(site)

        if not any([intelligence["json_ld"], intelligence["api_responses"], intelligence["html"]]):
            self.log.warning("[smartextract] No data collected from %s", site.name)
            return []

        # Phase 2: Strategy selection via LLM
        strategy = self._select_strategy(intelligence, site)

        if not strategy:
            self.log.warning("[smartextract] LLM could not determine extraction strategy for %s", site.name)
            return []

        # Phase 3: Extract jobs based on strategy
        return self._extract_jobs(strategy, intelligence, site)

    async def _collect_intelligence(self, site: CareerSite) -> dict:
        """Phase 1: Load page, intercept APIs, extract JSON-LD, capture HTML."""
        intelligence = {
            "json_ld": [],
            "api_responses": [],
            "html": "",
        }

        async with self.browser.new_page() as page:
            # Set up API response interception
            captured_json: list[dict] = []

            async def handle_response(response):
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    try:
                        body = await response.json()
                        # Only capture responses that look like job listings
                        body_str = json.dumps(body)
                        job_keywords = ["job", "position", "title", "company", "location"]
                        if any(kw in body_str.lower()[:2000] for kw in job_keywords):
                            captured_json.append({
                                "url": response.url,
                                "data": body,
                            })
                    except Exception:
                        pass

            page.on("response", handle_response)

            try:
                await page.goto(site.url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                self.log.warning("[smartextract] Page load timeout/error for %s: %s", site.name, e)
                try:
                    await page.goto(site.url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    return intelligence

            # Scroll to trigger lazy loading
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(0.5)

            # Extract JSON-LD scripts
            json_ld_scripts = await page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                    return Array.from(scripts).map(s => s.textContent);
                }
            """)

            for script_text in json_ld_scripts:
                try:
                    data = json.loads(script_text)
                    intelligence["json_ld"].append(data)
                except json.JSONDecodeError:
                    pass

            # Capture cleaned HTML (capped)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            # Remove scripts and styles
            for tag in soup(["script", "style", "noscript", "svg", "img"]):
                tag.decompose()
            cleaned = soup.get_text(separator="\n", strip=True)
            intelligence["html"] = cleaned[:MAX_HTML_SIZE]

            intelligence["api_responses"] = captured_json

        return intelligence

    def _select_strategy(self, intelligence: dict, site: CareerSite) -> dict | None:
        """Phase 2: Ask LLM to select extraction strategy."""
        # Build intelligence summary
        summary_parts = []

        if intelligence["json_ld"]:
            summary_parts.append(
                f"JSON-LD data found ({len(intelligence['json_ld'])} blocks):\n"
                f"{json.dumps(intelligence['json_ld'][:2], indent=2)[:2000]}"
            )

        if intelligence["api_responses"]:
            summary_parts.append(
                f"API JSON responses intercepted ({len(intelligence['api_responses'])} responses):\n"
            )
            for resp in intelligence["api_responses"][:3]:
                resp_preview = json.dumps(resp["data"], indent=2)[:1000]
                summary_parts.append(f"URL: {resp['url']}\nData preview: {resp_preview}")

        if intelligence["html"]:
            summary_parts.append(
                f"Page text (first 2000 chars):\n{intelligence['html'][:2000]}"
            )

        if not summary_parts:
            return None

        intelligence_text = "\n\n---\n\n".join(summary_parts)

        system = (
            "You analyze career page data and choose an extraction strategy. "
            "Return ONLY valid JSON with your strategy choice.\n\n"
            "Available strategies:\n"
            '1. {"strategy": "json_ld"} — if JSON-LD contains JobPosting data\n'
            '2. {"strategy": "api_response", "response_index": 0, "jobs_path": "data.jobs"} '
            "— if an intercepted API response contains job listings (specify the JSON path)\n"
            '3. {"strategy": "css_selectors", "container": ".job-card", "title": "h3", '
            '"company": ".company", "location": ".location", "link": "a"} '
            "— if the page uses consistent HTML structure for job cards\n"
            '4. {"strategy": "none"} — if no jobs can be extracted'
        )

        prompt = f"""Analyze this career page intelligence from {site.name} ({site.url}) and choose the best extraction strategy.

{intelligence_text}

Return ONLY the JSON strategy object."""

        try:
            response = self.ollama.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            text = response["message"]["content"]

            # Parse JSON from response
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                strategy = json.loads(json_match.group(0))
                if strategy.get("strategy") != "none":
                    return strategy
        except Exception as e:
            self.log.error("[smartextract] Strategy selection failed: %s", e)

        return None

    def _extract_jobs(self, strategy: dict, intelligence: dict, site: CareerSite) -> list[JobListing]:
        """Extract jobs based on the chosen strategy."""
        method = strategy.get("strategy")

        if method == "json_ld":
            return self._extract_from_json_ld(intelligence["json_ld"], site)
        elif method == "api_response":
            return self._extract_from_api(strategy, intelligence["api_responses"], site)
        elif method == "css_selectors":
            return self._extract_from_html(strategy, intelligence["html"], site)
        else:
            return []

    def _extract_from_json_ld(self, json_ld_blocks: list, site: CareerSite) -> list[JobListing]:
        """Extract jobs from JSON-LD JobPosting structured data."""
        jobs = []

        for block in json_ld_blocks:
            # Handle @graph arrays
            items = []
            if isinstance(block, list):
                items = block
            elif isinstance(block, dict):
                if block.get("@type") == "JobPosting":
                    items = [block]
                elif "@graph" in block:
                    items = [i for i in block["@graph"] if i.get("@type") == "JobPosting"]
                elif "itemListElement" in block:
                    items = [i.get("item", i) for i in block["itemListElement"]
                             if isinstance(i, dict)]

            for item in items:
                if not isinstance(item, dict):
                    continue
                title = item.get("title", item.get("name", ""))
                if not title:
                    continue

                company = ""
                org = item.get("hiringOrganization", {})
                if isinstance(org, dict):
                    company = org.get("name", "")
                elif isinstance(org, str):
                    company = org

                location = ""
                loc = item.get("jobLocation", {})
                if isinstance(loc, dict):
                    addr = loc.get("address", {})
                    if isinstance(addr, dict):
                        parts = [addr.get("addressLocality", ""), addr.get("addressRegion", "")]
                        location = ", ".join(p for p in parts if p)
                elif isinstance(loc, str):
                    location = loc

                url = item.get("url", site.url)
                description = item.get("description", "")
                # Strip HTML from description
                if "<" in description:
                    description = BeautifulSoup(description, "lxml").get_text(separator=" ", strip=True)

                salary = ""
                base_salary = item.get("baseSalary", {})
                if isinstance(base_salary, dict):
                    value = base_salary.get("value", {})
                    if isinstance(value, dict):
                        salary = f"{base_salary.get('currency', '')} {value.get('minValue', '')}-{value.get('maxValue', '')}"

                raw_id = f"smartextract:{site.name}:{url}"
                external_id = hashlib.md5(raw_id.encode()).hexdigest()[:16]

                jobs.append(JobListing(
                    platform="smartextract",
                    external_id=external_id,
                    title=title,
                    company=company or site.name,
                    location=location,
                    salary_info=salary or None,
                    description=description[:5000],
                    listing_url=url,
                    apply_url=url,
                ))

        return jobs

    def _extract_from_api(self, strategy: dict, api_responses: list, site: CareerSite) -> list[JobListing]:
        """Extract jobs from an intercepted API response using LLM-provided path."""
        idx = strategy.get("response_index", 0)
        if idx >= len(api_responses):
            return []

        response_data = api_responses[idx]["data"]
        jobs_path = strategy.get("jobs_path", "")

        # Navigate the JSON path to find the jobs array
        data = response_data
        if jobs_path:
            for key in jobs_path.split("."):
                if isinstance(data, dict):
                    data = data.get(key, data)
                elif isinstance(data, list) and key.isdigit():
                    data = data[int(key)] if int(key) < len(data) else data

        if not isinstance(data, list):
            # Try to extract with LLM
            return self._llm_extract_from_json(response_data, site)

        jobs = []
        for item in data:
            if not isinstance(item, dict):
                continue

            # Common field name patterns
            title = (
                item.get("title") or item.get("name") or item.get("jobTitle")
                or item.get("position") or ""
            )
            if not title:
                continue

            company = (
                item.get("company") or item.get("company_name")
                or item.get("organization") or site.name
            )
            location = (
                item.get("location") or item.get("city")
                or item.get("locations", "") or ""
            )
            if isinstance(location, list):
                location = ", ".join(str(l) for l in location)

            url = item.get("url") or item.get("apply_url") or item.get("link") or site.url
            description = item.get("description") or item.get("summary") or ""

            raw_id = f"smartextract:{site.name}:{title}:{company}"
            external_id = hashlib.md5(raw_id.encode()).hexdigest()[:16]

            jobs.append(JobListing(
                platform="smartextract",
                external_id=external_id,
                title=str(title),
                company=str(company),
                location=str(location),
                salary_info=None,
                description=str(description)[:5000],
                listing_url=str(url),
                apply_url=str(url),
            ))

        return jobs

    def _llm_extract_from_json(self, data: dict, site: CareerSite) -> list[JobListing]:
        """Fallback: ask LLM to extract jobs from arbitrary JSON."""
        preview = json.dumps(data, indent=2)[:3000]

        prompt = f"""Extract job listings from this JSON data from {site.name}.
Return a JSON array of objects with fields: title, company, location, url, description.

Data:
{preview}

Return ONLY a JSON array. If no jobs found, return []."""

        try:
            response = self.ollama.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response["message"]["content"]
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                items = json.loads(match.group(0))
                jobs = []
                for item in items:
                    title = item.get("title", "")
                    if not title:
                        continue
                    raw_id = f"smartextract:{site.name}:{title}"
                    external_id = hashlib.md5(raw_id.encode()).hexdigest()[:16]
                    jobs.append(JobListing(
                        platform="smartextract",
                        external_id=external_id,
                        title=title,
                        company=item.get("company", site.name),
                        location=item.get("location", ""),
                        salary_info=None,
                        description=item.get("description", "")[:5000],
                        listing_url=item.get("url", site.url),
                        apply_url=item.get("url", site.url),
                    ))
                return jobs
        except Exception as e:
            self.log.error("[smartextract] LLM JSON extraction failed: %s", e)

        return []

    def _extract_from_html(self, strategy: dict, html_text: str, site: CareerSite) -> list[JobListing]:
        """Extract jobs from page text using LLM with CSS selector hints."""
        # Since we only have cleaned text (not full HTML), use LLM to extract from text
        prompt = f"""Extract job listings from this career page text from {site.name}.
Return a JSON array of objects with fields: title, company, location, url.

Page text:
{html_text[:4000]}

Return ONLY a JSON array. If no jobs found, return []."""

        try:
            response = self.ollama.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response["message"]["content"]
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                items = json.loads(match.group(0))
                jobs = []
                for item in items:
                    title = item.get("title", "")
                    if not title:
                        continue
                    raw_id = f"smartextract:{site.name}:{title}"
                    external_id = hashlib.md5(raw_id.encode()).hexdigest()[:16]
                    jobs.append(JobListing(
                        platform="smartextract",
                        external_id=external_id,
                        title=title,
                        company=item.get("company", site.name),
                        location=item.get("location", ""),
                        salary_info=None,
                        description="",
                        listing_url=item.get("url", site.url),
                        apply_url=item.get("url", site.url),
                    ))
                return jobs
        except Exception as e:
            self.log.error("[smartextract] LLM HTML extraction failed: %s", e)

        return []

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
