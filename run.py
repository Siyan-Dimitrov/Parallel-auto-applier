#!/usr/bin/env python3
"""CLI entry point for the job application bot."""
from __future__ import annotations

import asyncio
import os
import sys

# Fix Windows console encoding for Unicode characters
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import click
from rich.table import Table

from src.config import load_config
from src.database import Database
from src.utils.logging import setup_logging, console


def _setup(config_path: str | None = None):
    """Load config, set up logging and database."""
    config = load_config()
    setup_logging(config.logging.level, config.logging.file)
    db = Database()
    return config, db


ALL_PLATFORMS = [
    "linkedin", "indeed", "glassdoor", "zip_recruiter", "google",
    "hiring_cafe", "workday_direct", "smartextract", "all",
]


@click.group()
def cli():
    """Job Application Bot - scrape, match, tailor, and apply to jobs automatically."""
    pass


@cli.command()
@click.option(
    "--platform",
    type=click.Choice(ALL_PLATFORMS),
    default="all",
    help="Platform to scrape from.",
)
def scrape(platform: str):
    """Scrape public job listings from supported platforms."""
    config, db = _setup()
    from src.main import run_scrape
    asyncio.run(run_scrape(config, db, platform))


@cli.command()
@click.option("--batch-size", type=int, default=0, help="Max number of jobs to match (0 = all unscored).")
def match(batch_size: int):
    """Run AI matching on unscored jobs using Ollama."""
    config, db = _setup()
    from src.main import run_match
    asyncio.run(run_match(config, db, batch_size=batch_size))


@cli.command()
@click.option("--max-jobs", type=int, default=0, help="Max number of jobs to tailor (0 = unlimited).")
def tailor(max_jobs: int):
    """Tailor resumes for matched jobs using AI with fabrication guard."""
    config, db = _setup()
    from src.main import run_tailor
    asyncio.run(run_tailor(config, db, max_jobs=max_jobs))


@cli.command()
@click.option("--dry-run", is_flag=True, help="Do everything except actually submitting applications.")
def apply(dry_run: bool):
    """Apply to matched jobs above the score threshold."""
    config, db = _setup()
    from src.main import run_apply
    asyncio.run(run_apply(config, db, dry_run=dry_run))


@cli.command()
@click.option("--dry-run", is_flag=True, help="Do everything except actually submitting applications.")
def run(dry_run: bool):
    """Full pipeline: scrape -> match -> tailor -> apply."""
    config, db = _setup()
    from src.main import run_full_pipeline
    asyncio.run(run_full_pipeline(config, db, dry_run=dry_run))


@cli.command()
def status():
    """Show job and application summary statistics."""
    config, db = _setup()
    stats = db.get_stats()

    # Summary table
    table = Table(title="Job Application Bot - Status", show_lines=True)
    table.add_column("Metric", style="bold cyan")
    table.add_column("Count", justify="right")

    table.add_row("Total Jobs Discovered", str(stats["total_jobs"]))
    table.add_row("Jobs Scored", str(stats["scored_jobs"]))
    table.add_row("Jobs Pending Scoring", str(stats["unscored_jobs"]))
    table.add_row("", "")
    table.add_row("Applications Pending", str(stats["apps_pending"]))
    table.add_row("Applications Submitted", str(stats["apps_submitted"]))
    table.add_row("Applications Failed", str(stats["apps_failed"]))
    table.add_row("Applications Skipped", str(stats["apps_skipped"]))

    console.print(table)

    # Platform breakdown
    if stats["by_platform"]:
        ptable = Table(title="Jobs by Platform", show_lines=True)
        ptable.add_column("Platform", style="bold")
        ptable.add_column("Jobs Found", justify="right")
        for platform, count in stats["by_platform"].items():
            ptable.add_row(platform, str(count))
        console.print(ptable)

    # Recent applications
    apps = db.get_applications()
    if apps:
        atable = Table(title="Recent Applications", show_lines=True)
        atable.add_column("Job Title", max_width=40)
        atable.add_column("Company", max_width=25)
        atable.add_column("Status")
        atable.add_column("Applied At")
        for app in apps[:20]:
            atable.add_row(
                app.get("title", "N/A"),
                app.get("company", "N/A"),
                app["status"],
                app.get("applied_at", "-") or "-",
            )
        console.print(atable)


if __name__ == "__main__":
    cli()
