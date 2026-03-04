"""Main orchestrator — ties together scraping, matching, tailoring, and applying."""
from __future__ import annotations

import asyncio
import random
import threading

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from src.ai_matcher import AIMatcher
from src.applicators.claude_code import ClaudeCodeApplicator
from src.browser import BrowserManager
from src.chrome import launch_chrome, kill_chrome, ensure_port_free, wait_for_cdp
from src.config import Config
from src.database import Database
from src.resume_parser import parse_resume
from src.utils.logging import console, get_logger


# ── Scrape ──────────────────────────────────────────────────────────────

async def run_scrape(config: Config, db: Database, platforms: list[str] | str = "all"):
    """Scrape job listings from selected platforms.

    Args:
        platforms: A list of platform keys to scrape, or "all" for every platform.
    """
    log = get_logger()

    # Normalize legacy single-string arg into a list
    if isinstance(platforms, str):
        if platforms == "all":
            platforms = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google",
                         "hiring_cafe", "workday_direct", "smartextract",
                         "adzuna", "careerjet"]
        else:
            platforms = [platforms]
    selected = set(platforms)

    # Phase 2: JobSpy scraper (no browser needed)
    jobspy_platforms = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google"]
    jobspy_selected = [p for p in jobspy_platforms if p in selected]

    if jobspy_selected:
        try:
            from src.scrapers.jobspy_scraper import JobSpyScraper
            scraper = JobSpyScraper(config, db)
            await scraper.scrape(platforms=jobspy_selected)
        except ImportError:
            log.warning("python-jobspy not installed — skipping JobSpy scraper. Run: pip install python-jobspy")
        except Exception as e:
            log.error("[jobspy] Scraper failed: %s", e)

    # Phase 3: Workday direct scraper (no browser needed)
    if "workday_direct" in selected:
        try:
            from src.scrapers.workday_scraper import WorkdayDirectScraper
            if config.employers.workday_employers:
                wd_scraper = WorkdayDirectScraper(config, db)
                await wd_scraper.scrape()
            else:
                log.info("[workday_direct] No employers configured — skipping. Add to config/employers.yaml")
        except ImportError:
            log.warning("Workday scraper dependencies missing.")
        except Exception as e:
            log.error("[workday_direct] Scraper failed: %s", e)

    # HiringCafe scraper (uses httpx, no browser)
    if "hiring_cafe" in selected:
        try:
            from src.scrapers.hiring_cafe import HiringCafeScraper
            browser = BrowserManager(config.browser)
            await browser.start()
            try:
                hc_scraper = HiringCafeScraper(config, db, browser)
                log.info("Starting hiring_cafe scraper...")
                await hc_scraper.scrape()
            finally:
                await browser.stop()
        except Exception as e:
            log.error("[hiring_cafe] Scraper failed: %s", e)

    # Adzuna scraper (uses httpx, no browser)
    if "adzuna" in selected:
        try:
            from src.scrapers.adzuna import AdzunaScraper
            browser = BrowserManager(config.browser)
            await browser.start()
            try:
                az_scraper = AdzunaScraper(config, db, browser)
                log.info("Starting adzuna scraper...")
                await az_scraper.scrape()
            finally:
                await browser.stop()
        except Exception as e:
            log.error("[adzuna] Scraper failed: %s", e)

    # Careerjet scraper (uses httpx, no browser)
    if "careerjet" in selected:
        try:
            from src.scrapers.careerjet import CareerjetScraper
            browser = BrowserManager(config.browser)
            await browser.start()
            try:
                cj_scraper = CareerjetScraper(config, db, browser)
                log.info("Starting careerjet scraper...")
                await cj_scraper.scrape()
            finally:
                await browser.stop()
        except Exception as e:
            log.error("[careerjet] Scraper failed: %s", e)

    # Phase 6: SmartExtract (needs browser)
    if "smartextract" in selected:
        try:
            from src.scrapers.smartextract import SmartExtractScraper
            if config.sites.career_pages:
                browser = BrowserManager(config.browser)
                await browser.start()
                try:
                    se_scraper = SmartExtractScraper(config, db, browser)
                    await se_scraper.scrape()
                finally:
                    await browser.stop()
            else:
                log.info("[smartextract] No career pages configured — skipping. Add to config/sites.yaml")
        except ImportError:
            log.warning("SmartExtract dependencies missing.")
        except Exception as e:
            log.error("[smartextract] Scraper failed: %s", e)

    stats = db.get_stats()
    console.print(f"\n[bold green]Scraping complete.[/] Total jobs in DB: {stats['total_jobs']}")


# ── Match ───────────────────────────────────────────────────────────────

async def run_match(config: Config, db: Database, cancel_event: threading.Event | None = None, batch_size: int = 0):
    """Score unscored jobs using Ollama AI with concurrent batch scoring.

    Args:
        batch_size: Maximum number of jobs to score in this run. 0 = all unscored.
    """
    log = get_logger()
    BATCH_SIZE = 10       # Jobs per LLM call
    CONCURRENCY = 3       # Parallel LLM calls

    # Parse resume
    resume_text = ""
    try:
        resume_text = parse_resume(config.application.resume_path)
    except FileNotFoundError:
        log.warning("Resume not found — matching will be less accurate")
    except Exception as e:
        log.error("Resume parsing failed: %s", e)

    ai = AIMatcher(config)
    total_unscored = len(db.get_unscored_jobs())
    unscored = db.get_unscored_jobs(limit=batch_size)

    if not unscored:
        console.print("[yellow]No unscored jobs found. Run 'scrape' first.[/]")
        return

    batch_msg = f" (batch of {len(unscored)}/{total_unscored})" if batch_size > 0 else ""
    console.print(f"Scoring {len(unscored)} jobs{batch_msg} with {config.ollama.match_model} "
                  f"(batches of {BATCH_SIZE}, {CONCURRENCY} concurrent)...")

    # Chunk jobs into batches
    batches = [unscored[i:i + BATCH_SIZE] for i in range(0, len(unscored), BATCH_SIZE)]

    matched = 0
    scored = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scoring jobs...", total=len(unscored))

        # Process batches in concurrent rounds
        for round_start in range(0, len(batches), CONCURRENCY):
            if cancel_event and cancel_event.is_set():
                log.info("Matching cancelled by user after scoring %d/%d jobs.", scored, len(unscored))
                console.print(f"[yellow]Matching stopped by user. Scored {scored}/{len(unscored)} jobs.[/]")
                break

            round_batches = batches[round_start:round_start + CONCURRENCY]
            batch_nums = f"{round_start + 1}-{round_start + len(round_batches)}/{len(batches)}"
            progress.update(task, description=f"Scoring batches {batch_nums}...")

            # Run batches concurrently via asyncio.to_thread (Ollama client is sync)
            async_tasks = [
                asyncio.to_thread(ai.score_jobs_batch, batch, resume_text)
                for batch in round_batches
            ]
            round_results = await asyncio.gather(*async_tasks, return_exceptions=True)

            for i, result in enumerate(round_results):
                if isinstance(result, Exception):
                    log.error("Batch %d failed: %s — scoring individually", round_start + i + 1, result)
                    # Fallback: score this batch individually
                    batch = round_batches[i]
                    result = []
                    for job in batch:
                        desc = job.get("description") or job.get("title", "")
                        r = ai.score_job(desc, resume_text, job_location=job.get("location", ""))
                        result.append({"job_id": job["id"], "score": r["score"], "reasoning": r["reasoning"]})

                # Save batch results
                db.update_job_scores_batch(result)
                for r in result:
                    scored += 1
                    if r["score"] >= config.job_preferences.min_match_score:
                        matched += 1
                progress.advance(task, advance=len(result))

    console.print(
        f"\n[bold green]Matching complete.[/] "
        f"{matched}/{scored} jobs above threshold "
        f"({config.job_preferences.min_match_score})"
    )


# ── Tailor ──────────────────────────────────────────────────────────────

async def run_tailor(config: Config, db: Database, max_jobs: int = 0):
    """Tailor resumes for matched jobs above the score threshold.

    Args:
        max_jobs: Maximum number of jobs to tailor. 0 = unlimited.
    """
    log = get_logger()

    resume_text = ""
    try:
        resume_text = parse_resume(config.application.resume_path)
    except FileNotFoundError:
        log.warning("Resume not found — cannot tailor.")
        console.print("[red]Resume not found — cannot tailor.[/]")
        return
    except Exception as e:
        log.error("Resume parsing failed: %s", e)
        return

    matched_jobs = db.get_matched_jobs(config.job_preferences.min_match_score)
    if not matched_jobs:
        log.info("No matched jobs to tailor resumes for. Run 'scrape' and 'match' first.")
        console.print("[yellow]No matched jobs to tailor resumes for. Run 'scrape' and 'match' first.[/]")
        return

    # Only tailor jobs that don't already have a tailored resume
    jobs_to_tailor = [j for j in matched_jobs if not j.get("tailored_resume")]
    if not jobs_to_tailor:
        log.info("All matched jobs already have tailored resumes.")
        console.print("[yellow]All matched jobs already have tailored resumes.[/]")
        return

    # Apply max_jobs limit
    if max_jobs > 0:
        jobs_to_tailor = jobs_to_tailor[:max_jobs]

    try:
        from src.resume_tailor import ResumeTailor
    except ImportError:
        console.print("[red]Resume tailor module not available.[/]")
        return

    tailor = ResumeTailor(config)
    console.print(f"Tailoring resumes for {len(jobs_to_tailor)} jobs...")

    tailored = 0
    failed = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Tailoring resumes...", total=len(jobs_to_tailor))

        for job in jobs_to_tailor:
            progress.update(task, description=f"Tailoring: {job['title'][:40]}...")

            result = tailor.tailor(
                original_resume=resume_text,
                job_description=job["description"] or job["title"],
                job_title=job["title"],
                company=job["company"] or "",
            )

            if result["passed_validation"]:
                db.update_tailored_resume(job["id"], result["tailored_resume"])
                tailored += 1
            else:
                log.warning(
                    "Tailoring failed validation for '%s' at %s: %s",
                    job["title"], job["company"], "; ".join(result["validation_issues"]),
                )
                # Save original as fallback
                db.update_tailored_resume(job["id"], resume_text)
                failed += 1

            progress.advance(task)

    console.print(
        f"\n[bold green]Tailoring complete.[/] "
        f"Tailored: {tailored} | Fell back to original: {failed}"
    )


# ── Apply ───────────────────────────────────────────────────────────────

async def _apply_worker(
    worker_id: int,
    job_queue: asyncio.Queue,
    applicator: ClaudeCodeApplicator,
    config: Config,
    db: Database,
    ai: AIMatcher,
    tailor,
    resume_text: str,
    dry_run: bool,
    counters: dict,
    total_jobs: int,
):
    """Worker coroutine that pulls jobs from the queue and applies."""
    log = get_logger()
    tag = f"[worker-{worker_id}]"

    while True:
        try:
            job = job_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        job_num = total_jobs - job_queue.qsize()
        log.info(
            "%s [%d/%d] Applying to: %s at %s (score: %.2f)",
            tag, job_num, total_jobs,
            job["title"], job["company"], job["match_score"],
        )

        # Generate cover letter (run in thread to avoid blocking event loop)
        cover_letter = None
        if config.application.generate_cover_letter and resume_text:
            try:
                cover_letter = await asyncio.to_thread(
                    ai.generate_cover_letter,
                    job["description"] or "", resume_text,
                    job["company"] or "", job["title"],
                    config.profile,
                )
            except Exception as e:
                log.warning("%s Cover letter generation failed: %s", tag, e)

        # Auto-tailor resume if not already tailored (run in thread)
        tailored_resume = job.get("tailored_resume")
        if not tailored_resume and tailor and resume_text:
            log.info("%s Tailoring resume for: %s at %s", tag, job["title"], job["company"])
            try:
                tailor_result = await asyncio.to_thread(
                    tailor.tailor,
                    original_resume=resume_text,
                    job_description=job["description"] or job["title"],
                    job_title=job["title"],
                    company=job["company"] or "",
                )
                if tailor_result["passed_validation"]:
                    tailored_resume = tailor_result["tailored_resume"]
                else:
                    log.warning(
                        "%s Tailoring failed validation for '%s': %s — using original resume.",
                        tag, job["title"], "; ".join(tailor_result["validation_issues"]),
                    )
                    tailored_resume = resume_text
                db.update_tailored_resume(job["id"], tailored_resume)
            except Exception as e:
                log.warning("%s Resume tailoring failed for '%s': %s — using original.", tag, job["title"], e)

        # Create application record
        app_id = db.create_application(job["id"], cover_letter, tailored_resume=tailored_resume)

        apply_url = job.get("apply_url") or job["listing_url"]

        # Apply using Claude Code
        result = await applicator.apply(
            apply_url=apply_url,
            job=job,
            resume_text=resume_text,
            tailored_resume_text=tailored_resume or "",
            cover_letter=cover_letter,
            dry_run=dry_run,
        )

        # Email verification retry — fetch code from inbox and re-launch agent
        if (
            not result.success
            and result.error_message == "email_verification_needed"
            and config.email.enabled
            and config.email.email
            and config.email.app_password
        ):
            log.info("%s Email verification needed — checking inbox...", tag)
            try:
                from src.utils.email_reader import EmailReader
                reader = EmailReader(
                    config.email.imap_host,
                    config.email.email,
                    config.email.app_password,
                )
                verification = await asyncio.to_thread(
                    reader.wait_for_verification, timeout=120, poll_interval=10,
                )
                if verification:
                    log.info(
                        "%s Found verification %s — retrying application...",
                        tag, verification["type"],
                    )
                    result = await applicator.apply(
                        apply_url=apply_url,
                        job=job,
                        resume_text=resume_text,
                        tailored_resume_text=tailored_resume or "",
                        cover_letter=cover_letter,
                        dry_run=dry_run,
                        verification_code=verification["value"] if verification["type"] == "code" else "",
                        verification_link=verification["value"] if verification["type"] == "link" else "",
                    )
                else:
                    log.warning("%s No verification email found within timeout.", tag)
            except Exception as e:
                log.error("%s Email verification retry failed: %s", tag, e)

        if result.success:
            status = "submitted" if not dry_run else "skipped"
            db.update_application(app_id, status, ats_type_used=result.ats_type)
            counters["submitted"] += 1
            log.info("%s Application %s: %s at %s", tag, status, job["title"], job["company"])
        else:
            db.update_application(
                app_id, "failed",
                ats_type_used=result.ats_type,
                error_message=result.error_message,
                failure_type=result.failure_type,
            )
            counters["failed"] += 1
            log.warning(
                "%s Application failed (%s): %s — %s",
                tag, result.failure_type or "unknown", job["title"], result.error_message,
            )

        # Random delay before next application
        if not job_queue.empty():
            delay_min, delay_max = config.application.delay_between_applications
            delay = random.uniform(delay_min, delay_max)
            log.info("%s Waiting %.0f seconds before next application...", tag, delay)
            await asyncio.sleep(delay)


async def run_apply(config: Config, db: Database, dry_run: bool = False):
    """Apply to matched jobs using parallel Claude Code workers + Chrome CDP."""
    log = get_logger()

    # Check daily limit
    daily_count = db.get_daily_application_count()
    max_daily = config.application.max_daily_applications
    remaining = max_daily - daily_count

    if remaining <= 0:
        console.print(f"[yellow]Daily application limit reached ({max_daily}). Try again tomorrow.[/]")
        return

    # Get matched jobs
    matched_jobs = db.get_matched_jobs(config.job_preferences.min_match_score)
    if not matched_jobs:
        console.print("[yellow]No matched jobs to apply to. Run 'scrape' and 'match' first.[/]")
        return

    jobs_to_apply = matched_jobs[:remaining]
    num_workers = max(1, min(config.application.num_workers, len(jobs_to_apply)))

    console.print(
        f"Applying to {len(jobs_to_apply)} jobs with {num_workers} worker(s) "
        f"({'DRY RUN' if dry_run else 'LIVE'})..."
    )

    # Parse resume
    resume_text = ""
    try:
        resume_text = parse_resume(config.application.resume_path)
    except Exception:
        pass

    # Prepare resume tailor for on-the-fly tailoring
    tailor = None
    if resume_text:
        try:
            from src.resume_tailor import ResumeTailor
            tailor = ResumeTailor(config)
        except ImportError:
            log.warning("Resume tailor module not available — will use original resume.")

    ai = AIMatcher(config)

    # Launch Chrome instances — one per worker
    base_port = config.cdp.base_port
    chrome_path = config.cdp.chrome_path or None
    chrome_procs = []
    applicators = []

    try:
        for i in range(num_workers):
            port = base_port + i
            profile_dir = f"{config.cdp.profile_dir}/worker_{i}"

            ensure_port_free(port)
            proc = launch_chrome(port=port, profile_dir=profile_dir, chrome_path=chrome_path)
            chrome_procs.append((port, proc))

            if not wait_for_cdp(port, timeout=15.0):
                console.print(f"[red]Chrome CDP failed to start on port {port}. Skipping worker {i}.[/]")
                continue

            applicators.append(ClaudeCodeApplicator(config, cdp_port=port))
            log.info("Worker %d ready on CDP port %d", i, port)

        if not applicators:
            console.print("[red]No Chrome workers started successfully.[/]")
            return

        # Populate job queue
        job_queue: asyncio.Queue = asyncio.Queue()
        for job in jobs_to_apply:
            job_queue.put_nowait(job)

        counters = {"submitted": 0, "failed": 0}

        # Launch worker coroutines
        workers = [
            _apply_worker(
                worker_id=i,
                job_queue=job_queue,
                applicator=applicators[i],
                config=config,
                db=db,
                ai=ai,
                tailor=tailor,
                resume_text=resume_text,
                dry_run=dry_run,
                counters=counters,
                total_jobs=len(jobs_to_apply),
            )
            for i in range(len(applicators))
        ]

        await asyncio.gather(*workers)

        console.print(
            f"\n[bold green]Application round complete.[/] "
            f"Submitted: {counters['submitted']} | Failed: {counters['failed']} | "
            f"Workers: {len(applicators)} | "
            f"{'(DRY RUN)' if dry_run else ''}"
        )

    finally:
        for port, proc in chrome_procs:
            kill_chrome(port)
            try:
                proc.terminate()
            except Exception:
                pass


# ── Full Pipeline ───────────────────────────────────────────────────────

async def run_full_pipeline(config: Config, db: Database, dry_run: bool = False):
    """Run the complete pipeline: scrape -> match -> apply (with auto-tailor)."""
    log = get_logger()

    console.rule("[bold blue]Step 1/3: Scraping Job Listings")
    await run_scrape(config, db, platform="all")

    console.rule("[bold blue]Step 2/3: AI Matching")
    await run_match(config, db)

    console.rule("[bold blue]Step 3/3: Applying to Jobs (with auto-tailor)")
    await run_apply(config, db, dry_run=dry_run)

    console.rule("[bold green]Pipeline Complete")
    _print_summary(db)


def _print_summary(db: Database):
    """Print a summary table of the current state."""
    stats = db.get_stats()

    table = Table(title="Run Summary", show_lines=True)
    table.add_column("Metric", style="bold cyan")
    table.add_column("Count", justify="right")

    table.add_row("Jobs Discovered", str(stats["total_jobs"]))
    table.add_row("Jobs Scored", str(stats["scored_jobs"]))
    table.add_row("Applications Submitted", str(stats["apps_submitted"]))
    table.add_row("Applications Failed", str(stats["apps_failed"]))

    console.print(table)

    if stats["by_platform"]:
        ptable = Table(title="By Platform")
        ptable.add_column("Platform", style="bold")
        ptable.add_column("Jobs", justify="right")
        for platform, count in stats["by_platform"].items():
            ptable.add_row(platform, str(count))
        console.print(ptable)
