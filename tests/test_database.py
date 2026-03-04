"""Tests for database operations."""
import tempfile
from pathlib import Path

import pytest

from src.database import Database


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    yield database
    database.close()


class TestJobOperations:
    def test_insert_job(self, db):
        job_id = db.insert_job(
            platform="linkedin",
            external_id="12345",
            title="Data Scientist",
            company="TestCorp",
            location="Remote",
            description="A great job",
            listing_url="https://example.com/job/12345",
        )
        assert job_id is not None
        assert job_id > 0

    def test_insert_duplicate_returns_none(self, db):
        kwargs = dict(
            platform="linkedin",
            external_id="12345",
            title="Data Scientist",
            company="TestCorp",
            location="Remote",
            description="A great job",
            listing_url="https://example.com/job/12345",
        )
        id1 = db.insert_job(**kwargs)
        id2 = db.insert_job(**kwargs)
        assert id1 is not None
        assert id2 is None

    def test_get_unscored_jobs(self, db):
        db.insert_job(
            platform="indeed", external_id="1",
            title="ML Engineer", company="A", location="NY",
            description="desc", listing_url="https://a.com/1",
        )
        db.insert_job(
            platform="indeed", external_id="2",
            title="AI Engineer", company="B", location="SF",
            description="desc2", listing_url="https://b.com/2",
        )
        unscored = db.get_unscored_jobs()
        assert len(unscored) == 2
        assert all(j["match_score"] is None for j in unscored)

    def test_update_job_score(self, db):
        job_id = db.insert_job(
            platform="linkedin", external_id="99",
            title="Data Scientist", company="X", location="Remote",
            description="test", listing_url="https://x.com/99",
        )
        db.update_job_score(job_id, 0.85, "Great match")
        job = db.get_job(job_id)
        assert job["match_score"] == 0.85
        assert job["match_reasoning"] == "Great match"

    def test_update_job_ats(self, db):
        job_id = db.insert_job(
            platform="linkedin", external_id="100",
            title="Engineer", company="Y", location="Remote",
            description="", listing_url="https://y.com/100",
        )
        db.update_job_ats(job_id, "greenhouse", "https://boards.greenhouse.io/y/100")
        job = db.get_job(job_id)
        assert job["ats_type"] == "greenhouse"
        assert job["apply_url"] == "https://boards.greenhouse.io/y/100"

    def test_get_matched_jobs(self, db):
        # Insert and score 3 jobs
        for i, score in enumerate([0.9, 0.5, 0.7]):
            jid = db.insert_job(
                platform="linkedin", external_id=str(i),
                title=f"Job {i}", company=f"Co {i}", location="Remote",
                description="", listing_url=f"https://example.com/{i}",
            )
            db.update_job_score(jid, score, "reason")

        matched = db.get_matched_jobs(0.6)
        assert len(matched) == 2  # 0.9 and 0.7
        assert matched[0]["match_score"] == 0.9  # sorted desc

    def test_get_all_jobs(self, db):
        db.insert_job(
            platform="indeed", external_id="a",
            title="Job A", company="Co", location="",
            description="", listing_url="https://a.com/a",
        )
        db.insert_job(
            platform="linkedin", external_id="b",
            title="Job B", company="Co2", location="",
            description="", listing_url="https://b.com/b",
        )
        all_jobs = db.get_all_jobs()
        assert len(all_jobs) == 2


class TestApplicationOperations:
    def test_create_and_update_application(self, db):
        job_id = db.insert_job(
            platform="linkedin", external_id="200",
            title="Dev", company="Z", location="Remote",
            description="", listing_url="https://z.com/200",
        )
        app_id = db.create_application(job_id, cover_letter="Dear Hiring Manager...")
        assert app_id > 0

        db.update_application(app_id, "submitted", ats_type_used="greenhouse")
        apps = db.get_applications("submitted")
        assert len(apps) == 1
        assert apps[0]["status"] == "submitted"
        assert apps[0]["ats_type_used"] == "greenhouse"
        assert apps[0]["applied_at"] is not None

    def test_failed_application(self, db):
        job_id = db.insert_job(
            platform="indeed", external_id="300",
            title="SRE", company="W", location="",
            description="", listing_url="https://w.com/300",
        )
        app_id = db.create_application(job_id)
        db.update_application(app_id, "failed", error_message="Form not found")
        apps = db.get_applications("failed")
        assert len(apps) == 1
        assert apps[0]["error_message"] == "Form not found"

    def test_daily_application_count(self, db):
        job_id = db.insert_job(
            platform="linkedin", external_id="400",
            title="Test", company="T", location="",
            description="", listing_url="https://t.com/400",
        )
        app_id = db.create_application(job_id)
        db.update_application(app_id, "submitted")
        count = db.get_daily_application_count()
        assert count == 1

    def test_matched_jobs_excludes_applied(self, db):
        """Jobs already applied to should not appear in matched results."""
        jid = db.insert_job(
            platform="linkedin", external_id="500",
            title="Applied Job", company="C", location="",
            description="", listing_url="https://c.com/500",
        )
        db.update_job_score(jid, 0.95, "Perfect match")
        db.create_application(jid)  # status='pending' by default

        matched = db.get_matched_jobs(0.6)
        assert len(matched) == 0  # excluded because it has a pending application


class TestSearchRuns:
    def test_search_run_lifecycle(self, db):
        run_id = db.start_search_run("linkedin", "Data Scientist - Remote")
        assert run_id > 0
        db.finish_search_run(run_id, jobs_found=25, jobs_matched=10)
        # No assertion needed — just verify no errors


class TestStats:
    def test_stats_empty_db(self, db):
        stats = db.get_stats()
        assert stats["total_jobs"] == 0
        assert stats["scored_jobs"] == 0
        assert stats["unscored_jobs"] == 0
        assert stats["apps_submitted"] == 0
        assert stats["by_platform"] == {}

    def test_stats_with_data(self, db):
        # Add some jobs
        for i in range(3):
            jid = db.insert_job(
                platform="linkedin", external_id=str(i),
                title=f"Job {i}", company="Co", location="",
                description="", listing_url=f"https://co.com/{i}",
            )
            if i < 2:
                db.update_job_score(jid, 0.5 + i * 0.2, "ok")

        jid2 = db.insert_job(
            platform="indeed", external_id="x",
            title="Indeed Job", company="Y", location="",
            description="", listing_url="https://y.com/x",
        )

        stats = db.get_stats()
        assert stats["total_jobs"] == 4
        assert stats["scored_jobs"] == 2
        assert stats["unscored_jobs"] == 2
        assert stats["by_platform"]["linkedin"] == 3
        assert stats["by_platform"]["indeed"] == 1
