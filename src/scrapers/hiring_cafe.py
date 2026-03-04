from __future__ import annotations

import asyncio
import base64
import json
import re
from urllib.parse import quote

import httpx

from src.scrapers.base import BaseScraper, JobListing, matches_location_preference


class HiringCafeScraper(BaseScraper):
    """Scrape hiring.cafe job listings via their internal REST API."""

    platform = "hiring_cafe"

    API_URL = "https://hiring.cafe/api/search-jobs"
    COUNT_URL = "https://hiring.cafe/api/search-jobs/get-total-count"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://hiring.cafe/search",
    }

    async def scrape(self) -> list[JobListing]:
        all_jobs: list[JobListing] = []
        queries = self._build_search_queries()

        async with httpx.AsyncClient(timeout=30, headers=self.HEADERS) as client:
            for query in queries:
                self.log.info(
                    "[hiring_cafe] Searching: %s in %s",
                    query["title"],
                    query["location"],
                )
                run_id = self.db.start_search_run(
                    "hiring_cafe", f"{query['title']} - {query['location']}"
                )

                try:
                    jobs = await self._search_api(
                        client, query["title"], query["location"]
                    )
                    all_jobs.extend(jobs)
                    self.db.finish_search_run(run_id, jobs_found=len(jobs))
                    self.log.info(
                        "[hiring_cafe] Found %d jobs for '%s'",
                        len(jobs),
                        query["title"],
                    )
                except Exception as e:
                    self.log.error(
                        "[hiring_cafe] Search failed for '%s': %s",
                        query["title"],
                        e,
                    )
                    self.db.finish_search_run(run_id, jobs_found=0)

                await asyncio.sleep(2)

        # Post-filter by location preference
        all_jobs = self.filter_by_location(all_jobs)

        new_count = self.save_jobs(all_jobs)
        self.log.info(
            "[hiring_cafe] Total: %d jobs after location filter, %d new", len(all_jobs), new_count
        )
        return all_jobs

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def _search_api(
        self, client: httpx.AsyncClient, title: str, location: str
    ) -> list[JobListing]:
        """Call the hiring.cafe search API and parse results."""
        search_state = self._build_search_state(title, location)
        encoded = self._encode_search_state(search_state)

        resp = await client.get(f"{self.API_URL}?s={encoded}")
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        return self._parse_results(results)

    def _build_search_state(self, title: str, location: str) -> dict:
        """Build the JSON search state payload for the API."""
        # Map location to workplace types
        loc_lower = location.lower()
        if "remote" in loc_lower:
            workplace_types = ["Remote"]
        elif "hybrid" in loc_lower:
            workplace_types = ["Remote", "Hybrid"]
        else:
            workplace_types = ["Remote", "Hybrid", "Onsite"]

        return {
            "searchQuery": title,
            "jobTitleQuery": "",
            "jobDescriptionQuery": "",
            "technologyKeywordsQuery": "",
            "requirementsKeywordsQuery": "",
            "locations": [],
            "defaultToUserLocation": False,
            "userLocation": None,
            "workplaceTypes": workplace_types,
            "currency": {"label": "Any", "value": None},
            "frequency": {"label": "Any", "value": None},
            "calcFrequency": "Yearly",
            "minCompensationLowEnd": None,
            "minCompensationHighEnd": None,
            "maxCompensationLowEnd": None,
            "maxCompensationHighEnd": None,
            "restrictJobsToTransparentSalaries": False,
            "commitmentTypes": ["Full Time", "Part Time", "Contract"],
            "seniorityLevel": [
                "Entry Level",
                "Mid Level",
                "Senior Level",
            ],
            "roleTypes": ["Individual Contributor", "People Manager"],
            "roleYoeRange": [0, 20],
            "excludeIfRoleYoeIsNotSpecified": False,
            "managementYoeRange": [0, 20],
            "excludeIfManagementYoeIsNotSpecified": False,
            "dateFetchedPastNDays": 30,
            "sortBy": "default",
            "departments": [],
            "hiddenCompanies": [],
            "hideJobTypes": [],
            "user": None,
            "companyNames": [],
            "excludedCompanyNames": [],
            "industries": [],
            "excludedIndustries": [],
            "companyKeywords": [],
            "excludedCompanyKeywords": [],
            "companyPublicOrPrivate": "all",
            "companySizeRanges": [],
            "isNonProfit": "all",
            "languageRequirements": [],
            "excludedLanguageRequirements": [],
            "languageRequirementsOperator": "OR",
            "applicationFormEase": [],
            "encouragedToApply": [],
            "benefitsAndPerks": [],
            "associatesDegreeFieldsOfStudy": [],
            "bachelorsDegreeFieldsOfStudy": [],
            "mastersDegreeFieldsOfStudy": [],
            "doctorateDegreeFieldsOfStudy": [],
            "excludedAssociatesDegreeFieldsOfStudy": [],
            "excludedBachelorsDegreeFieldsOfStudy": [],
            "excludedMastersDegreeFieldsOfStudy": [],
            "excludedDoctorateDegreeFieldsOfStudy": [],
            "associatesDegreeRequirements": [],
            "bachelorsDegreeRequirements": [],
            "mastersDegreeRequirements": [],
            "doctorateDegreeRequirements": [],
            "licensesAndCertifications": [],
            "excludedLicensesAndCertifications": [],
            "physicalEnvironments": [],
            "physicalLaborIntensity": [],
            "physicalPositions": [],
            "oralCommunicationLevels": [],
            "computerUsageLevels": [],
            "cognitiveDemandLevels": [],
            "securityClearances": [],
            "airTravelRequirement": [],
            "landTravelRequirement": [],
            "morningShiftWork": [],
            "eveningShiftWork": [],
            "overnightShiftWork": [],
            "weekendAvailabilityRequired": "Doesn't Matter",
            "holidayAvailabilityRequired": "Doesn't Matter",
            "overtimeRequired": "Doesn't Matter",
            "onCallRequirements": [],
            "minYearFounded": None,
            "maxYearFounded": None,
            "latestInvestmentYearRange": [None, None],
            "latestInvestmentSeries": [],
            "excludedLatestInvestmentSeries": [],
            "latestInvestmentAmount": None,
            "investors": [],
            "excludedInvestors": [],
            "usaGovPref": None,
        }

    @staticmethod
    def _encode_search_state(state: dict) -> str:
        """Encode search state dict to the base64(url-encode(json)) format the API expects."""
        json_str = json.dumps(state, separators=(",", ":"))
        url_encoded = quote(json_str)
        return base64.b64encode(url_encoded.encode()).decode()

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_results(self, results: list[dict]) -> list[JobListing]:
        """Convert API result dicts into JobListing objects."""
        jobs: list[JobListing] = []

        for item in results:
            try:
                job = self._parse_single_result(item)
                if job:
                    jobs.append(job)
            except Exception as e:
                self.log.debug("[hiring_cafe] Failed to parse result: %s", e)

        return jobs

    def _parse_single_result(self, item: dict) -> JobListing | None:
        """Parse a single API result into a JobListing."""
        external_id = item.get("id") or item.get("objectID", "")
        if not external_id:
            return None

        apply_url = item.get("apply_url", "")
        if not apply_url:
            return None

        # Extract job title — prefer job_information, fall back to v5 data
        ji = item.get("job_information", {})
        v5 = item.get("v5_processed_job_data", {})

        title = (
            ji.get("job_title_raw")
            or ji.get("title")
            or v5.get("core_job_title")
            or ""
        )
        if not title:
            return None

        company = v5.get("company_name") or "Unknown"
        if company in ("undefined", "null", ""):
            company = "Unknown"

        # Build location from workplace type and countries
        workplace = v5.get("workplace_type", "")
        countries = v5.get("workplace_countries", [])
        location_parts = []
        if workplace:
            location_parts.append(workplace)
        if countries:
            location_parts.append(", ".join(countries))
        location = " — ".join(location_parts) if location_parts else ""

        # Extract salary info if transparent
        salary_info = self._extract_salary(v5)

        # Get description (HTML) — strip tags for storage
        description_html = ji.get("description", "")
        description = self._strip_html(description_html)

        # Build a listing URL on hiring.cafe for the job
        listing_url = f"https://hiring.cafe/viewjob/{external_id}"

        return JobListing(
            platform="hiring_cafe",
            external_id=external_id,
            title=title,
            company=company,
            location=location,
            salary_info=salary_info,
            description=description[:5000],  # cap at 5000 chars
            listing_url=listing_url,
            apply_url=apply_url,
        )

    @staticmethod
    def _extract_salary(v5: dict) -> str | None:
        """Extract salary string from v5 processed data if available."""
        if not v5.get("is_compensation_transparent"):
            return None

        currency = v5.get("listed_compensation_currency", "")
        yearly_min = v5.get("yearly_min")
        yearly_max = v5.get("yearly_max")

        if yearly_min and yearly_max:
            return f"{currency} {yearly_min:,.0f} - {yearly_max:,.0f}/yr"
        elif yearly_min:
            return f"{currency} {yearly_min:,.0f}+/yr"
        elif yearly_max:
            return f"Up to {currency} {yearly_max:,.0f}/yr"
        return None

    @staticmethod
    def _strip_html(html: str) -> str:
        """Remove HTML tags from a string."""
        if not html:
            return ""
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
