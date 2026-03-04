"""Tests for scraper parsing logic (no browser required)."""
import pytest

from src.scrapers.linkedin import LinkedInScraper
from src.scrapers.indeed import IndeedScraper
from src.scrapers.hiring_cafe import HiringCafeScraper
from src.scrapers.base import JobListing


class TestLinkedInParser:
    def test_parse_job_card(self):
        html = """
        <div class="base-card">
            <h3 class="base-search-card__title">Senior Data Scientist</h3>
            <h4 class="base-search-card__subtitle">TechCorp</h4>
            <span class="job-search-card__location">San Francisco, CA</span>
            <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/123456?trk=test"></a>
        </div>
        """
        scraper = LinkedInScraper.__new__(LinkedInScraper)
        import logging
        scraper.log = logging.getLogger("test")
        jobs = scraper._parse_listings(html)
        assert len(jobs) == 1
        assert jobs[0].title == "Senior Data Scientist"
        assert jobs[0].company == "TechCorp"
        assert jobs[0].external_id == "123456"
        assert jobs[0].platform == "linkedin"

    def test_parse_empty_html(self):
        scraper = LinkedInScraper.__new__(LinkedInScraper)
        import logging
        scraper.log = logging.getLogger("test")
        jobs = scraper._parse_listings("<html><body></body></html>")
        assert jobs == []

    def test_parse_card_missing_link(self):
        html = """
        <div class="base-card">
            <h3 class="base-search-card__title">Data Engineer</h3>
            <h4 class="base-search-card__subtitle">SomeCo</h4>
        </div>
        """
        scraper = LinkedInScraper.__new__(LinkedInScraper)
        import logging
        scraper.log = logging.getLogger("test")
        jobs = scraper._parse_listings(html)
        assert jobs == []  # No link = skip


class TestIndeedParser:
    def test_parse_job_card(self):
        html = """
        <div class="job_seen_beacon">
            <h2 class="jobTitle"><a class="jcs-JobTitle" href="/rc/clk?jk=abc123">ML Engineer</a></h2>
            <span class="companyName">AI Corp</span>
            <div class="companyLocation">Remote</div>
            <div class="salary-snippet-container">$120k - $180k</div>
            <div class="job-snippet">Work on cutting-edge AI systems</div>
        </div>
        """
        scraper = IndeedScraper.__new__(IndeedScraper)
        import logging
        scraper.log = logging.getLogger("test")
        jobs = scraper._parse_listings(html)
        assert len(jobs) == 1
        assert jobs[0].title == "ML Engineer"
        assert jobs[0].company == "AI Corp"
        assert jobs[0].salary_info == "$120k - $180k"
        assert jobs[0].external_id == "abc123"

    def test_parse_empty(self):
        scraper = IndeedScraper.__new__(IndeedScraper)
        import logging
        scraper.log = logging.getLogger("test")
        jobs = scraper._parse_listings("<html></html>")
        assert jobs == []


class TestHiringCafeParser:
    def _make_scraper(self):
        import logging
        scraper = HiringCafeScraper.__new__(HiringCafeScraper)
        scraper.log = logging.getLogger("test")
        return scraper

    def test_parse_single_result(self):
        scraper = self._make_scraper()
        item = {
            "id": "greenhouse___acme___12345",
            "objectID": "greenhouse___acme___12345",
            "apply_url": "https://job-boards.greenhouse.io/acme/jobs/12345",
            "source": "greenhouse",
            "board_token": "acme",
            "job_information": {
                "job_title_raw": "Senior ML Engineer",
                "title": "Senior ML Engineer",
                "description": "<p>Build <strong>cool</strong> models.</p>",
            },
            "v5_processed_job_data": {
                "core_job_title": "Senior ML Engineer",
                "company_name": "Acme Corp",
                "workplace_type": "Remote",
                "workplace_countries": ["US"],
                "is_compensation_transparent": True,
                "listed_compensation_currency": "USD",
                "yearly_min": 150000,
                "yearly_max": 200000,
            },
        }
        job = scraper._parse_single_result(item)
        assert job is not None
        assert job.title == "Senior ML Engineer"
        assert job.company == "Acme Corp"
        assert job.location == "Remote — US"
        assert job.apply_url == "https://job-boards.greenhouse.io/acme/jobs/12345"
        assert job.salary_info == "USD 150,000 - 200,000/yr"
        assert "Build" in job.description
        assert "<p>" not in job.description  # HTML stripped

    def test_parse_result_missing_apply_url(self):
        scraper = self._make_scraper()
        item = {
            "id": "test_123",
            "apply_url": "",
            "job_information": {"job_title_raw": "Data Scientist"},
            "v5_processed_job_data": {"company_name": "TestCo"},
        }
        job = scraper._parse_single_result(item)
        assert job is None

    def test_parse_result_undefined_company(self):
        scraper = self._make_scraper()
        item = {
            "id": "test_456",
            "apply_url": "https://example.com/apply",
            "job_information": {"job_title_raw": "AI Engineer"},
            "v5_processed_job_data": {"company_name": "undefined"},
        }
        job = scraper._parse_single_result(item)
        assert job is not None
        assert job.company == "Unknown"

    def test_encode_decode_search_state(self):
        scraper = self._make_scraper()
        state = {"searchQuery": "Data Scientist", "locations": []}
        encoded = scraper._encode_search_state(state)
        # Should be valid base64
        import base64
        from urllib.parse import unquote
        decoded = unquote(base64.b64decode(encoded).decode())
        import json
        parsed = json.loads(decoded)
        assert parsed["searchQuery"] == "Data Scientist"

    def test_strip_html(self):
        scraper = self._make_scraper()
        assert scraper._strip_html("<p>Hello <b>world</b></p>") == "Hello world"
        assert scraper._strip_html("") == ""
        assert scraper._strip_html(None) == ""

    def test_parse_results_batch(self):
        scraper = self._make_scraper()
        results = [
            {
                "id": f"test_{i}",
                "apply_url": f"https://example.com/apply/{i}",
                "job_information": {"job_title_raw": f"Job {i}"},
                "v5_processed_job_data": {"company_name": f"Company {i}"},
            }
            for i in range(5)
        ]
        jobs = scraper._parse_results(results)
        assert len(jobs) == 5


class TestJobListing:
    def test_job_listing_creation(self):
        job = JobListing(
            platform="test",
            external_id="1",
            title="Engineer",
            company="Co",
            location="Remote",
            salary_info=None,
            description="A job",
            listing_url="https://example.com",
        )
        assert job.platform == "test"
        assert job.apply_url is None  # optional
