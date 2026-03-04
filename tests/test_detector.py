"""Tests for ATS detection."""
import pytest

from src.applicators.detector import detect_ats_from_url


class TestATSDetection:
    def test_greenhouse_url(self):
        assert detect_ats_from_url("https://boards.greenhouse.io/company/jobs/123") == "greenhouse"
        assert detect_ats_from_url("https://company.greenhouse.io/jobs/123") == "greenhouse"

    def test_lever_url(self):
        assert detect_ats_from_url("https://jobs.lever.co/company/abc-def") == "lever"
        assert detect_ats_from_url("https://company.lever.co/senior-engineer") == "lever"

    def test_workday_url(self):
        assert detect_ats_from_url("https://company.wd5.myworkdayjobs.com/en-US/careers") == "workday"
        assert detect_ats_from_url("https://workday.com/apply/123") == "workday"

    def test_bamboohr_url(self):
        assert detect_ats_from_url("https://company.bamboohr.com/jobs/view.php?id=100") == "bamboohr"

    def test_generic_url(self):
        assert detect_ats_from_url("https://company.com/careers/apply") == "generic"
        assert detect_ats_from_url("https://randomats.io/job/123") == "generic"

    def test_case_insensitive(self):
        assert detect_ats_from_url("https://BOARDS.GREENHOUSE.IO/Company/123") == "greenhouse"
        assert detect_ats_from_url("https://jobs.LEVER.co/company") == "lever"
