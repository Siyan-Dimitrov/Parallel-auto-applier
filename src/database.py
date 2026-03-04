from __future__ import annotations

import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any

from src.utils.logging import get_logger

DB_PATH = Path("data/bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    external_id TEXT,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    salary_info TEXT,
    description TEXT,
    listing_url TEXT NOT NULL,
    apply_url TEXT,
    ats_type TEXT,
    match_score REAL,
    match_reasoning TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, external_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    status TEXT DEFAULT 'pending',
    ats_type_used TEXT,
    cover_letter TEXT,
    error_message TEXT,
    applied_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT,
    query TEXT,
    jobs_found INTEGER DEFAULT 0,
    jobs_matched INTEGER DEFAULT 0,
    applications_submitted INTEGER DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._migrate()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        # Indexes for frequently filtered columns
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_match_score ON jobs(match_score)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_location ON jobs(location)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_applied_at ON applications(applied_at)")
        self.conn.commit()

    def _migrate(self):
        """Run safe migrations for new columns."""
        # Phase 5: Add tailored_resume column to applications
        columns = [
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(applications)").fetchall()
        ]
        if "tailored_resume" not in columns:
            self.conn.execute("ALTER TABLE applications ADD COLUMN tailored_resume TEXT")
            self.conn.commit()
            get_logger().info("Migration: added tailored_resume column to applications table.")

        # Add tailored_resume column to jobs (for pre-computed tailored resumes)
        job_columns = [
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        ]
        if "tailored_resume" not in job_columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN tailored_resume TEXT")
            self.conn.commit()
            get_logger().info("Migration: added tailored_resume column to jobs table.")

        # Add failure_type column to applications (permanent/transient classification)
        if "failure_type" not in columns:
            self.conn.execute("ALTER TABLE applications ADD COLUMN failure_type TEXT")
            self.conn.commit()
            get_logger().info("Migration: added failure_type column to applications table.")

    def close(self):
        self.conn.close()

    # ── Jobs ────────────────────────────────────────────────────────────

    def insert_job(self, **kwargs) -> int | None:
        """Insert a job, returning its id. Returns None if duplicate."""
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        try:
            cur = self.conn.execute(
                f"INSERT INTO jobs ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            get_logger().debug("Duplicate job skipped: %s / %s", kwargs.get("platform"), kwargs.get("external_id"))
            return None

    def get_unscored_jobs(self, limit: int = 0) -> list[dict]:
        """Get jobs that haven't been scored yet.

        Args:
            limit: Maximum number of jobs to return. 0 = unlimited.
        """
        if limit > 0:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE match_score IS NULL ORDER BY discovered_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE match_score IS NULL ORDER BY discovered_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_job_score(self, job_id: int, score: float, reasoning: str):
        self.conn.execute(
            "UPDATE jobs SET match_score = ?, match_reasoning = ? WHERE id = ?",
            (score, reasoning, job_id),
        )
        self.conn.commit()

    def update_job_scores_batch(self, results: list[dict]):
        """Batch-update scores for multiple jobs in a single transaction.

        Args:
            results: List of dicts with keys: job_id, score, reasoning.
        """
        self.conn.executemany(
            "UPDATE jobs SET match_score = ?, match_reasoning = ? WHERE id = ?",
            [(r["score"], r["reasoning"], r["job_id"]) for r in results],
        )
        self.conn.commit()

    def update_job_ats(self, job_id: int, ats_type: str, apply_url: str | None = None):
        if apply_url:
            self.conn.execute(
                "UPDATE jobs SET ats_type = ?, apply_url = ? WHERE id = ?",
                (ats_type, apply_url, job_id),
            )
        else:
            self.conn.execute(
                "UPDATE jobs SET ats_type = ? WHERE id = ?",
                (ats_type, job_id),
            )
        self.conn.commit()

    def update_tailored_resume(self, job_id: int, tailored_resume: str):
        """Store a tailored resume for a specific job."""
        self.conn.execute(
            "UPDATE jobs SET tailored_resume = ? WHERE id = ?",
            (tailored_resume, job_id),
        )
        self.conn.commit()

    def get_matched_jobs(self, min_score: float, max_failures: int = 2) -> list[dict]:
        """Get jobs above threshold that haven't been successfully applied to,
        haven't exceeded the retry limit, and have no permanent failures."""
        rows = self.conn.execute(
            """SELECT j.* FROM jobs j
               WHERE j.match_score >= ?
               AND j.id NOT IN (SELECT job_id FROM applications WHERE status IN ('submitted', 'pending'))
               AND j.id NOT IN (SELECT job_id FROM applications WHERE failure_type = 'permanent')
               AND (SELECT COUNT(*) FROM applications WHERE job_id = j.id AND status = 'failed') < ?
               ORDER BY j.match_score DESC""",
            (min_score, max_failures),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_all_jobs(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY discovered_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ── Applications ────────────────────────────────────────────────────

    def create_application(self, job_id: int, cover_letter: str | None = None, tailored_resume: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO applications (job_id, cover_letter, tailored_resume) VALUES (?, ?, ?)",
            (job_id, cover_letter, tailored_resume),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_application(self, app_id: int, status: str, ats_type_used: str | None = None,
                           error_message: str | None = None, failure_type: str | None = None):
        applied_at = datetime.now().isoformat() if status == "submitted" else None
        self.conn.execute(
            """UPDATE applications
               SET status = ?, ats_type_used = ?, error_message = ?, applied_at = ?, failure_type = ?
               WHERE id = ?""",
            (status, ats_type_used, error_message, applied_at, failure_type, app_id),
        )
        self.conn.commit()

    def get_daily_application_count(self) -> int:
        today = date.today().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM applications WHERE status = 'submitted' AND DATE(applied_at) = ?",
            (today,),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_applications(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT a.*, j.title, j.company FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.status = ?",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT a.*, j.title, j.company FROM applications a JOIN jobs j ON a.job_id = j.id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Search Runs ─────────────────────────────────────────────────────

    def start_search_run(self, platform: str, query: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO search_runs (platform, query) VALUES (?, ?)",
            (platform, query),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_search_run(self, run_id: int, jobs_found: int, jobs_matched: int = 0, applications_submitted: int = 0):
        self.conn.execute(
            """UPDATE search_runs
               SET jobs_found = ?, jobs_matched = ?, applications_submitted = ?, finished_at = ?
               WHERE id = ?""",
            (jobs_found, jobs_matched, applications_submitted, datetime.now().isoformat(), run_id),
        )
        self.conn.commit()

    # ── Clear ────────────────────────────────────────────────────────────

    def clear_all_jobs(self):
        """Delete all jobs, applications, and search runs."""
        self.conn.execute("DELETE FROM applications")
        self.conn.execute("DELETE FROM jobs")
        self.conn.execute("DELETE FROM search_runs")
        self.conn.commit()

    # ── Stats ───────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Get summary statistics for status display."""
        stats = {}
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM jobs").fetchone()
        stats["total_jobs"] = row["cnt"]

        row = self.conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE match_score IS NOT NULL").fetchone()
        stats["scored_jobs"] = row["cnt"]

        row = self.conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE match_score IS NULL").fetchone()
        stats["unscored_jobs"] = row["cnt"]

        for status in ("pending", "submitted", "failed", "skipped"):
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM applications WHERE status = ?", (status,)
            ).fetchone()
            stats[f"apps_{status}"] = row["cnt"]

        # Per-platform breakdown
        platforms = self.conn.execute(
            "SELECT platform, COUNT(*) as cnt FROM jobs GROUP BY platform"
        ).fetchall()
        stats["by_platform"] = {r["platform"]: r["cnt"] for r in platforms}

        return stats
