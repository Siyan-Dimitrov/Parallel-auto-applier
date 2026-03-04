#!/usr/bin/env python3
"""GUI dashboard for the Job Application Bot."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import queue
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
try:
    from ttkbootstrap.widgets.scrolled import ScrolledText, ScrolledFrame
    from ttkbootstrap.widgets.tableview import Tableview
except ImportError:
    from ttkbootstrap.scrolled import ScrolledText, ScrolledFrame
    from ttkbootstrap.tableview import Tableview

import yaml

from src.config import load_config, Config, CONFIG_PATH, EXAMPLE_CONFIG_PATH
from src.database import Database
from src.utils.logging import setup_logging, get_logger


# ── Logging bridge ─────────────────────────────────────────────────────
class _QueueLogHandler(logging.Handler):
    """Routes Python logging records into the GUI's log queue for real-time display."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self._q = log_queue

    def emit(self, record: logging.LogRecord):
        try:
            self._q.put((record.levelname, self.format(record)))
        except Exception:
            pass

# ── Async bridge ────────────────────────────────────────────────────────

class AsyncRunner:
    """Runs async pipeline steps in a background thread, posting logs to a queue."""

    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.cancel_event = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run(self, coro_func, *args, on_done=None):
        if self.running:
            self.log_queue.put(("WARNING", "A task is already running."))
            return
        self.cancel_event.clear()
        self._loop = None
        self._task = None

        def _target():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._task = loop.create_task(coro_func(*args))
                loop.run_until_complete(self._task)
                if self.cancel_event.is_set():
                    self.log_queue.put(("INFO", "Task was stopped by user."))
                else:
                    self.log_queue.put(("DONE", "Task completed successfully."))
            except asyncio.CancelledError:
                self.log_queue.put(("INFO", "Task stopped by user."))
            except Exception as e:
                if self.cancel_event.is_set():
                    self.log_queue.put(("INFO", "Task stopped by user."))
                else:
                    self.log_queue.put(("ERROR", f"Task failed: {e}"))
            finally:
                self._task = None
                self._loop = None
                loop.close()
                if on_done:
                    on_done()
        self._thread = threading.Thread(target=_target, daemon=True)
        self._thread.start()

    def cancel(self):
        """Cancel the running async task immediately."""
        self.cancel_event.set()
        task = self._task
        loop = self._loop
        if task and loop and not task.done():
            loop.call_soon_threadsafe(task.cancel)
            self.log_queue.put(("INFO", "Stop requested — cancelling task..."))
        else:
            self.log_queue.put(("WARNING", "No task is currently running."))


# ── Main App ────────────────────────────────────────────────────────────

class AppBotGUI:
    def __init__(self):
        self.root = ttk.Window(
            title="Job Application Bot",
            themename="darkly",
            size=(1100, 750),
            minsize=(900, 600),
        )
        self.root.place_window_center()

        self.log_queue: queue.Queue = queue.Queue()
        self.runner = AsyncRunner(self.log_queue)
        self.db: Database | None = None
        self.config: Config | None = None

        # Bridge Python logging → GUI log queue so background tasks show real-time output
        self._log_handler = _QueueLogHandler(self.log_queue)
        self._log_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(self._log_handler)

        self._build_ui()
        self._try_load_config()
        self._poll_log_queue()

    # ── UI Construction ─────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=X)
        ttk.Label(top, text="Job Application Bot", font=("Segoe UI", 18, "bold"),
                  bootstyle="inverse-primary", padding=(15, 8)).pack(side=LEFT)

        self.status_label = ttk.Label(top, text="No config loaded", bootstyle="warning")
        self.status_label.pack(side=RIGHT, padx=10)

        self.stop_btn = ttk.Button(top, text="Stop", bootstyle="danger",
                                   command=self._stop_task, width=8)
        # Hidden by default — shown when a task is running

        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root, bootstyle="dark")
        self.notebook.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))

        self._build_dashboard_tab()
        self._build_config_tab()
        self._build_scrape_tab()
        self._build_match_tab()
        self._build_apply_tab()
        self._build_jobs_tab()
        self._build_log_tab()

    # ── Dashboard Tab ───────────────────────────────────────────────────

    def _build_dashboard_tab(self):
        frame = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(frame, text="  Dashboard  ")

        # Stats cards row
        cards_frame = ttk.Frame(frame)
        cards_frame.pack(fill=X, pady=(0, 15))

        self.stat_vars = {}
        card_defs = [
            ("Total Jobs", "total_jobs", "info"),
            ("Scored", "scored_jobs", "primary"),
            ("Unscored", "unscored_jobs", "warning"),
            ("Applied", "apps_submitted", "success"),
            ("Failed", "apps_failed", "danger"),
            ("Pending", "apps_pending", "secondary"),
        ]
        for i, (label, key, style) in enumerate(card_defs):
            cards_frame.columnconfigure(i, weight=1)
            card = ttk.Frame(cards_frame, bootstyle=style, padding=15)
            card.grid(row=0, column=i, padx=5, sticky=NSEW)
            ttk.Label(card, text=label, font=("Segoe UI", 10), bootstyle=f"inverse-{style}").pack()
            var = ttk.StringVar(value="0")
            self.stat_vars[key] = var
            ttk.Label(card, textvariable=var, font=("Segoe UI", 28, "bold"),
                      bootstyle=f"inverse-{style}").pack()

        # Action buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, pady=10)

        ttk.Button(btn_frame, text="Full Pipeline (Scrape > Match > Apply)",
                   bootstyle="success", command=self._run_pipeline, width=40).pack(side=LEFT, padx=5)
        ttk.Button(btn_frame, text="Dry Run (No Submit)",
                   bootstyle="warning-outline", command=self._run_pipeline_dry, width=25).pack(side=LEFT, padx=5)
        ttk.Button(btn_frame, text="Clear All Jobs",
                   bootstyle="danger-outline", command=self._clear_all_jobs, width=14).pack(side=RIGHT, padx=5)
        ttk.Button(btn_frame, text="Refresh Stats",
                   bootstyle="info-outline", command=self._refresh_stats, width=15).pack(side=RIGHT, padx=5)

        # Scraped jobs table
        self.dash_jobs_frame = ttk.LabelFrame(frame, text="Scraped Jobs")
        self.dash_jobs_frame.pack(fill=BOTH, expand=True, pady=5)
        self._dash_jobs_col_defs = [
            {"text": "Title", "stretch": True, "width": 200},
            {"text": "Company", "stretch": True, "width": 130},
            {"text": "Location", "stretch": False, "width": 130},
            {"text": "Platform", "stretch": False, "width": 80},
            {"text": "Score", "stretch": False, "width": 60},
            {"text": "ATS", "stretch": False, "width": 80},
        ]
        self.dash_jobs_table = Tableview(
            self.dash_jobs_frame, coldata=self._dash_jobs_col_defs, rowdata=[],
            paginated=True, pagesize=20, searchable=True, bootstyle="info",
        )
        self.dash_jobs_table.pack(fill=BOTH, expand=True)

    # ── Config Tab ──────────────────────────────────────────────────────

    def _build_config_tab(self):
        wrapper = ttk.Frame(self.notebook)
        self.notebook.add(wrapper, text="  Config  ")
        sf = ScrolledFrame(wrapper, autohide=True)
        sf.pack(fill=BOTH, expand=True)
        frame = sf

        # Personal info section
        sec = ttk.LabelFrame(frame, text="Personal Info")
        sec.pack(fill=X, padx=10, pady=8)

        self.cfg_vars = {}
        personal_fields = [
            ("Full Name", "full_name"),
            ("Email", "email"),
            ("Phone", "phone"),
            ("LinkedIn URL", "linkedin_url"),
            ("Website", "website"),
            ("Current Company", "current_company"),
            ("Years Experience", "years_experience"),
            ("Notice Period", "notice_period"),
            ("Password", "password"),
            ("Address", "address"),
            ("City", "city"),
            ("State", "state"),
            ("Zip Code", "zip_code"),
        ]
        for i, (label, key) in enumerate(personal_fields):
            ttk.Label(sec, text=label, width=18).grid(row=i, column=0, sticky=W, pady=3)
            var = ttk.StringVar()
            self.cfg_vars[f"personal_{key}"] = var
            entry_kwargs = {"textvariable": var, "width": 50}
            if key == "password":
                entry_kwargs["show"] = "*"
            ttk.Entry(sec, **entry_kwargs).grid(row=i, column=1, sticky=EW, padx=5, pady=3)
        sec.columnconfigure(1, weight=1)

        # Job preferences
        sec2 = ttk.LabelFrame(frame, text="Job Preferences")
        sec2.pack(fill=X, padx=10, pady=8)

        ttk.Label(sec2, text="Job Titles (one per line)").pack(anchor=W)
        self.titles_text = ttk.Text(sec2, height=5, width=60)
        self.titles_text.pack(fill=X, pady=(0, 8))

        row = ttk.Frame(sec2)
        row.pack(fill=X)
        ttk.Label(row, text="Locations:").pack(side=LEFT)
        self.cfg_vars["locations"] = ttk.StringVar(value="Remote")
        ttk.Entry(row, textvariable=self.cfg_vars["locations"], width=40).pack(side=LEFT, padx=5)

        ttk.Label(row, text="Min Score:").pack(side=LEFT, padx=(15, 0))
        self.cfg_vars["min_score"] = ttk.StringVar(value="0.6")
        ttk.Entry(row, textvariable=self.cfg_vars["min_score"], width=8).pack(side=LEFT, padx=5)

        ttk.Label(row, text="Min Salary (£):").pack(side=LEFT, padx=(15, 0))
        self.cfg_vars["min_salary"] = ttk.StringVar(value="")
        ttk.Entry(row, textvariable=self.cfg_vars["min_salary"], width=10).pack(side=LEFT, padx=5)

        # Ollama settings
        sec3 = ttk.LabelFrame(frame, text="Ollama Settings")
        sec3.pack(fill=X, padx=10, pady=8)

        r = ttk.Frame(sec3)
        r.pack(fill=X)
        ttk.Label(r, text="Model:").pack(side=LEFT)
        self.cfg_vars["ollama_model"] = ttk.StringVar(value="llama3.1")
        ttk.Entry(r, textvariable=self.cfg_vars["ollama_model"], width=20).pack(side=LEFT, padx=5)
        ttk.Label(r, text="Match Model:").pack(side=LEFT, padx=(15, 0))
        self.cfg_vars["ollama_match_model"] = ttk.StringVar(value="kimi-k2.5:cloud")
        ttk.Entry(r, textvariable=self.cfg_vars["ollama_match_model"], width=20).pack(side=LEFT, padx=5)

        r_url = ttk.Frame(sec3)
        r_url.pack(fill=X, pady=(3, 0))
        ttk.Label(r_url, text="URL:").pack(side=LEFT)
        self.cfg_vars["ollama_url"] = ttk.StringVar(value="http://localhost:11434")
        ttk.Entry(r_url, textvariable=self.cfg_vars["ollama_url"], width=30).pack(side=LEFT, padx=5)

        # Application settings
        sec4 = ttk.LabelFrame(frame, text="Application Settings")
        sec4.pack(fill=X, padx=10, pady=8)

        r2 = ttk.Frame(sec4)
        r2.pack(fill=X, pady=3)
        ttk.Label(r2, text="Max Daily Apps:").pack(side=LEFT)
        self.cfg_vars["max_daily"] = ttk.StringVar(value="50")
        ttk.Entry(r2, textvariable=self.cfg_vars["max_daily"], width=8).pack(side=LEFT, padx=5)

        ttk.Label(r2, text="Delay (min-max sec):").pack(side=LEFT, padx=(15, 0))
        self.cfg_vars["delay_min"] = ttk.StringVar(value="30")
        ttk.Entry(r2, textvariable=self.cfg_vars["delay_min"], width=6).pack(side=LEFT, padx=2)
        ttk.Label(r2, text="-").pack(side=LEFT)
        self.cfg_vars["delay_max"] = ttk.StringVar(value="90")
        ttk.Entry(r2, textvariable=self.cfg_vars["delay_max"], width=6).pack(side=LEFT, padx=2)

        r3 = ttk.Frame(sec4)
        r3.pack(fill=X, pady=3)
        self.cfg_vars["cover_letter"] = ttk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="Generate cover letters", variable=self.cfg_vars["cover_letter"],
                        bootstyle="round-toggle").pack(side=LEFT)
        self.cfg_vars["headless"] = ttk.BooleanVar(value=False)
        ttk.Checkbutton(r3, text="Headless browser", variable=self.cfg_vars["headless"],
                        bootstyle="round-toggle").pack(side=LEFT, padx=20)

        r4 = ttk.Frame(sec4)
        r4.pack(fill=X, pady=3)
        ttk.Label(r4, text="Resume PDF:").pack(side=LEFT)
        self.cfg_vars["resume_path"] = ttk.StringVar(value="resume/resume.pdf")
        ttk.Entry(r4, textvariable=self.cfg_vars["resume_path"], width=40).pack(side=LEFT, padx=5)
        ttk.Button(r4, text="Browse...", command=self._browse_resume,
                   bootstyle="secondary-outline").pack(side=LEFT)

        # Chrome CDP settings
        sec_cdp = ttk.LabelFrame(frame, text="Chrome CDP Settings")
        sec_cdp.pack(fill=X, padx=10, pady=8)

        cdp_r1 = ttk.Frame(sec_cdp)
        cdp_r1.pack(fill=X, pady=3)
        ttk.Label(cdp_r1, text="Chrome Path:").pack(side=LEFT)
        self.cfg_vars["cdp_chrome_path"] = ttk.StringVar(value="")
        ttk.Entry(cdp_r1, textvariable=self.cfg_vars["cdp_chrome_path"], width=40).pack(side=LEFT, padx=5)
        ttk.Label(cdp_r1, text="(leave empty to auto-detect)", bootstyle="secondary",
                  font=("Segoe UI", 8)).pack(side=LEFT, padx=5)

        cdp_r2 = ttk.Frame(sec_cdp)
        cdp_r2.pack(fill=X, pady=3)
        ttk.Label(cdp_r2, text="CDP Port:").pack(side=LEFT)
        self.cfg_vars["cdp_base_port"] = ttk.StringVar(value="9222")
        ttk.Entry(cdp_r2, textvariable=self.cfg_vars["cdp_base_port"], width=8).pack(side=LEFT, padx=5)
        ttk.Label(cdp_r2, text="Profile Dir:").pack(side=LEFT, padx=(15, 0))
        self.cfg_vars["cdp_profile_dir"] = ttk.StringVar(value="data/chrome_profiles")
        ttk.Entry(cdp_r2, textvariable=self.cfg_vars["cdp_profile_dir"], width=30).pack(side=LEFT, padx=5)

        # CAPTCHA settings
        sec_captcha = ttk.LabelFrame(frame, text="CAPTCHA Solving")
        sec_captcha.pack(fill=X, padx=10, pady=8)

        cap_r1 = ttk.Frame(sec_captcha)
        cap_r1.pack(fill=X, pady=3)
        self.cfg_vars["captcha_enabled"] = ttk.BooleanVar(value=False)
        ttk.Checkbutton(cap_r1, text="Enable CAPTCHA solving", variable=self.cfg_vars["captcha_enabled"],
                        bootstyle="round-toggle").pack(side=LEFT)
        ttk.Label(cap_r1, text="Provider:").pack(side=LEFT, padx=(20, 0))
        self.cfg_vars["captcha_provider"] = ttk.StringVar(value="capsolver")
        ttk.Combobox(cap_r1, textvariable=self.cfg_vars["captcha_provider"], width=12,
                     values=["capsolver", "2captcha"]).pack(side=LEFT, padx=5)

        cap_r2 = ttk.Frame(sec_captcha)
        cap_r2.pack(fill=X, pady=3)
        ttk.Label(cap_r2, text="API Key:").pack(side=LEFT)
        self.cfg_vars["captcha_api_key"] = ttk.StringVar(value="")
        ttk.Entry(cap_r2, textvariable=self.cfg_vars["captcha_api_key"], width=50, show="*").pack(side=LEFT, padx=5)

        # Email verification settings
        sec_email = ttk.LabelFrame(frame, text="Email Verification (Gmail IMAP)")
        sec_email.pack(fill=X, padx=10, pady=8)

        email_r1 = ttk.Frame(sec_email)
        email_r1.pack(fill=X, pady=3)
        self.cfg_vars["email_enabled"] = ttk.BooleanVar(value=False)
        ttk.Checkbutton(email_r1, text="Enable email verification reader",
                        variable=self.cfg_vars["email_enabled"],
                        bootstyle="round-toggle").pack(side=LEFT)

        email_r2 = ttk.Frame(sec_email)
        email_r2.pack(fill=X, pady=3)
        ttk.Label(email_r2, text="IMAP Email:").pack(side=LEFT)
        self.cfg_vars["email_address"] = ttk.StringVar(value="")
        ttk.Entry(email_r2, textvariable=self.cfg_vars["email_address"], width=35).pack(side=LEFT, padx=5)
        ttk.Label(email_r2, text="App Password:").pack(side=LEFT, padx=(15, 0))
        self.cfg_vars["email_app_password"] = ttk.StringVar(value="")
        ttk.Entry(email_r2, textvariable=self.cfg_vars["email_app_password"], width=30, show="*").pack(side=LEFT, padx=5)

        # API Scrapers settings (Adzuna, Careerjet)
        sec_api = ttk.LabelFrame(frame, text="API Scrapers (Adzuna / Careerjet)")
        sec_api.pack(fill=X, padx=10, pady=8)

        api_r1 = ttk.Frame(sec_api)
        api_r1.pack(fill=X, pady=3)
        ttk.Label(api_r1, text="Adzuna App ID:").pack(side=LEFT)
        self.cfg_vars["adzuna_app_id"] = ttk.StringVar(value="")
        ttk.Entry(api_r1, textvariable=self.cfg_vars["adzuna_app_id"], width=20).pack(side=LEFT, padx=5)
        ttk.Label(api_r1, text="Adzuna App Key:").pack(side=LEFT, padx=(15, 0))
        self.cfg_vars["adzuna_app_key"] = ttk.StringVar(value="")
        ttk.Entry(api_r1, textvariable=self.cfg_vars["adzuna_app_key"], width=40, show="*").pack(side=LEFT, padx=5)

        api_r2 = ttk.Frame(sec_api)
        api_r2.pack(fill=X, pady=3)
        ttk.Label(api_r2, text="Careerjet Affiliate ID:").pack(side=LEFT)
        self.cfg_vars["careerjet_affid"] = ttk.StringVar(value="")
        ttk.Entry(api_r2, textvariable=self.cfg_vars["careerjet_affid"], width=30).pack(side=LEFT, padx=5)
        ttk.Label(api_r2, text="(get from developer.adzuna.com / careerjet.com/partners/api)",
                  bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=5)

        # Resume preview section
        sec5 = ttk.LabelFrame(frame, text="Resume - Parsed Info (review & edit)")
        sec5.pack(fill=BOTH, expand=True, padx=10, pady=8)

        resume_btn_row = ttk.Frame(sec5)
        resume_btn_row.pack(fill=X, pady=(5, 5), padx=5)
        ttk.Button(resume_btn_row, text="Parse Resume", bootstyle="info",
                   command=self._parse_and_preview_resume, width=15).pack(side=LEFT)
        ttk.Button(resume_btn_row, text="Auto-fill from Resume", bootstyle="warning-outline",
                   command=self._autofill_from_resume, width=20).pack(side=LEFT, padx=10)
        self.resume_status_var = ttk.StringVar(value="")
        ttk.Label(resume_btn_row, textvariable=self.resume_status_var,
                  bootstyle="secondary").pack(side=LEFT, padx=10)

        self.resume_preview = ttk.Text(sec5, height=10, width=60, wrap="word")
        self.resume_preview.pack(fill=BOTH, expand=True, padx=5, pady=(0, 5))

        # Save button
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=X, padx=10, pady=15)
        ttk.Button(btn_row, text="Save Config", bootstyle="success", command=self._save_config,
                   width=20).pack(side=LEFT)
        ttk.Button(btn_row, text="Reload Config", bootstyle="info-outline", command=self._try_load_config,
                   width=15).pack(side=LEFT, padx=10)

    # ── Scrape Tab ──────────────────────────────────────────────────────

    def _build_scrape_tab(self):
        frame = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(frame, text="  Scrape  ")

        ttk.Label(frame, text="Scrape public job listings", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        ttk.Label(frame, text="Discovers jobs from LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google, hiring.cafe, Workday, and career pages.",
                  bootstyle="secondary").pack(anchor=W, pady=(0, 10))

        # Platform selection (multi-select checkboxes)
        pf = ttk.LabelFrame(frame, text="Platforms")
        pf.pack(fill=X, pady=5)
        platforms = [
            ("linkedin", "LinkedIn"),
            ("indeed", "Indeed"),
            ("glassdoor", "Glassdoor"),
            ("zip_recruiter", "ZipRecruiter"),
            ("google", "Google Jobs"),
            ("hiring_cafe", "hiring.cafe"),
            ("workday_direct", "Workday Direct"),
            ("smartextract", "SmartExtract"),
            ("adzuna", "Adzuna"),
            ("careerjet", "Careerjet"),
        ]
        self.scrape_platform_vars: dict[str, ttk.BooleanVar] = {}
        row1 = ttk.Frame(pf)
        row1.pack(fill=X)
        row2 = ttk.Frame(pf)
        row2.pack(fill=X)
        for i, (val, label) in enumerate(platforms):
            var = ttk.BooleanVar(value=True)
            self.scrape_platform_vars[val] = var
            parent = row1 if i < 5 else row2
            ttk.Checkbutton(parent, text=label, variable=var).pack(side=LEFT, padx=8, pady=2)
        # Select All / Deselect All toggle
        btn_frame = ttk.Frame(pf)
        btn_frame.pack(fill=X, pady=(2, 4))
        ttk.Button(btn_frame, text="Select All", bootstyle="link",
                   command=lambda: [v.set(True) for v in self.scrape_platform_vars.values()]).pack(side=LEFT, padx=8)
        ttk.Button(btn_frame, text="Deselect All", bootstyle="link",
                   command=lambda: [v.set(False) for v in self.scrape_platform_vars.values()]).pack(side=LEFT, padx=4)

        # Search filters
        filt = ttk.LabelFrame(frame, text="Search Filters")
        filt.pack(fill=X, pady=5)

        # Row 1: Location + Country + Distance
        r1 = ttk.Frame(filt)
        r1.pack(fill=X, padx=8, pady=4)
        ttk.Label(r1, text="Location:").pack(side=LEFT)
        self.scrape_location_var = ttk.StringVar(value="")
        ttk.Entry(r1, textvariable=self.scrape_location_var, width=25).pack(side=LEFT, padx=5)
        ttk.Label(r1, text="(overrides config locations for this scrape)", bootstyle="secondary",
                  font=("Segoe UI", 8)).pack(side=LEFT, padx=(0, 15))

        ttk.Label(r1, text="Country:").pack(side=LEFT)
        self.scrape_country_var = ttk.StringVar(value="UK")
        country_combo = ttk.Combobox(r1, textvariable=self.scrape_country_var, width=10,
                                     values=["UK", "USA", "Canada", "Germany", "France",
                                             "Netherlands", "Ireland", "Australia", "India", "Remote"])
        country_combo.pack(side=LEFT, padx=5)

        ttk.Label(r1, text="Distance (mi):").pack(side=LEFT, padx=(15, 0))
        self.scrape_distance_var = ttk.StringVar(value="50")
        ttk.Spinbox(r1, from_=5, to=500, textvariable=self.scrape_distance_var, width=5).pack(side=LEFT, padx=5)

        # Row 2: Remote + Job Type + Results + Hours Old
        r2 = ttk.Frame(filt)
        r2.pack(fill=X, padx=8, pady=4)

        self.scrape_remote_var = ttk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Remote only", variable=self.scrape_remote_var,
                        bootstyle="success-round-toggle").pack(side=LEFT, padx=(0, 15))

        ttk.Label(r2, text="Job type:").pack(side=LEFT)
        self.scrape_job_type_var = ttk.StringVar(value="")
        type_combo = ttk.Combobox(r2, textvariable=self.scrape_job_type_var, width=12,
                                  values=["", "fulltime", "parttime", "contract", "internship"])
        type_combo.pack(side=LEFT, padx=5)

        ttk.Label(r2, text="Max results:").pack(side=LEFT, padx=(15, 0))
        self.scrape_results_var = ttk.StringVar(value="50")
        ttk.Spinbox(r2, from_=5, to=500, textvariable=self.scrape_results_var, width=5).pack(side=LEFT, padx=5)

        ttk.Label(r2, text="Hours old:").pack(side=LEFT, padx=(15, 0))
        self.scrape_hours_var = ttk.StringVar(value="72")
        ttk.Spinbox(r2, from_=1, to=720, textvariable=self.scrape_hours_var, width=5).pack(side=LEFT, padx=5)

        # Buttons
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=X, pady=8)
        ttk.Button(btn_row, text="Start Scraping", bootstyle="primary", command=self._run_scrape,
                   width=20).pack(side=LEFT)
        ttk.Button(btn_row, text="Stop", bootstyle="danger-outline", command=self._stop_task,
                   width=8).pack(side=LEFT, padx=10)

        self.scrape_log = ScrolledText(frame, height=12, autohide=True)
        self.scrape_log.pack(fill=BOTH, expand=True)

    # ── Match Tab ───────────────────────────────────────────────────────

    def _build_match_tab(self):
        frame = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(frame, text="  Match  ")
        self.match_frame = frame

        ttk.Label(frame, text="AI Job Matching", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        ttk.Label(frame, text="Uses Ollama to score how well each job matches your resume.",
                  bootstyle="secondary").pack(anchor=W, pady=(0, 10))

        info = ttk.Frame(frame)
        info.pack(fill=X, pady=5)
        self.match_info_var = ttk.StringVar(value="Click 'Refresh' to load scored jobs.")
        ttk.Label(info, textvariable=self.match_info_var, font=("Segoe UI", 11)).pack(side=LEFT)
        ttk.Button(info, text="Refresh", bootstyle="info-outline", command=self._refresh_match_info,
                   width=10).pack(side=RIGHT)

        batch_row = ttk.Frame(frame)
        batch_row.pack(fill=X, pady=(0, 5))
        ttk.Label(batch_row, text="Batch size:").pack(side=LEFT, padx=(0, 5))
        self.match_batch_var = ttk.StringVar(value="0")
        ttk.Spinbox(batch_row, from_=0, to=9999, textvariable=self.match_batch_var,
                     width=6).pack(side=LEFT, padx=5)
        ttk.Label(batch_row, text="(0 = match all unscored jobs)",
                  bootstyle="secondary").pack(side=LEFT, padx=5)

        match_btn_row = ttk.Frame(frame)
        match_btn_row.pack(fill=X, pady=10)
        ttk.Button(match_btn_row, text="Run AI Matching", bootstyle="primary", command=self._run_match,
                   width=20).pack(side=LEFT, padx=5)
        ttk.Button(match_btn_row, text="Stop Matching", bootstyle="danger-outline", command=self._stop_task,
                   width=15).pack(side=LEFT, padx=5)

        # Scored jobs table
        self._match_col_defs = [
            {"text": "Score", "stretch": False, "width": 65},
            {"text": "Title", "stretch": True, "width": 220},
            {"text": "Company", "stretch": True, "width": 150},
            {"text": "Location", "stretch": False, "width": 120},
            {"text": "Platform", "stretch": False, "width": 80},
            {"text": "Reasoning", "stretch": True, "width": 250},
        ]
        self.match_table = Tableview(
            frame, coldata=self._match_col_defs, rowdata=[], paginated=True, pagesize=25,
            searchable=True, bootstyle="primary",
        )
        self.match_table.pack(fill=BOTH, expand=True)

        self.match_log = ScrolledText(frame, height=6, autohide=True)
        self.match_log.pack(fill=X, pady=(5, 0))

    # ── Apply Tab ───────────────────────────────────────────────────────

    def _build_apply_tab(self):
        frame = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(frame, text="  Apply  ")
        self.apply_frame = frame

        ttk.Label(frame, text="Auto-Apply to Jobs", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        ttk.Label(frame, text="Fills and submits application forms on company career pages. "
                  "Browser window will open so you can watch applications being submitted.",
                  bootstyle="secondary").pack(anchor=W, pady=(0, 10))

        # Options row
        opts = ttk.Frame(frame)
        opts.pack(fill=X, pady=5)
        self.dry_run_var = ttk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Dry Run (fill forms but don't submit)",
                        variable=self.dry_run_var, bootstyle="warning-round-toggle").pack(side=LEFT)

        # Workers control
        ttk.Label(opts, text="  Workers:").pack(side=LEFT, padx=(15, 2))
        self.workers_var = ttk.StringVar(value=str(getattr(self.config.application, "num_workers", 1) if self.config else 1))
        ttk.Spinbox(opts, from_=1, to=6, textvariable=self.workers_var, width=3).pack(side=LEFT)

        self.apply_info_var = ttk.StringVar(value="")
        ttk.Label(opts, textvariable=self.apply_info_var).pack(side=RIGHT)

        # Apply controls
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=X, pady=5)
        ttk.Button(btn_row, text="Start Applying", bootstyle="success", command=self._run_apply,
                   width=20).pack(side=LEFT)
        ttk.Button(btn_row, text="Stop", bootstyle="danger-outline", command=self._stop_task,
                   width=8).pack(side=LEFT, padx=10)
        ttk.Button(btn_row, text="Refresh", bootstyle="info-outline", command=self._refresh_apply_info,
                   width=10).pack(side=LEFT, padx=5)

        # Paned area: matched jobs (top) + application history (bottom)
        pane = ttk.Panedwindow(frame, orient="vertical")
        pane.pack(fill=BOTH, expand=True, pady=(5, 0))

        # Top pane — matched jobs ready to apply
        top_pane = ttk.LabelFrame(pane, text="Jobs Ready to Apply")
        pane.add(top_pane, weight=1)
        self._apply_col_defs = [
            {"text": "Score", "stretch": False, "width": 60},
            {"text": "Title", "stretch": True, "width": 200},
            {"text": "Company", "stretch": True, "width": 130},
            {"text": "Location", "stretch": False, "width": 110},
            {"text": "Platform", "stretch": False, "width": 75},
            {"text": "Tailored", "stretch": False, "width": 60},
        ]
        self.apply_table_parent = top_pane
        self.apply_table = Tableview(
            top_pane, coldata=self._apply_col_defs, rowdata=[], paginated=False,
            searchable=True, bootstyle="success", height=15,
        )
        self.apply_table.pack(fill=BOTH, expand=True)

        # Bottom pane — application history
        bot_pane = ttk.LabelFrame(pane, text="Application History")
        pane.add(bot_pane, weight=1)
        self._history_col_defs = [
            {"text": "Title", "stretch": True, "width": 200},
            {"text": "Company", "stretch": True, "width": 130},
            {"text": "Status", "stretch": False, "width": 80},
            {"text": "ATS", "stretch": False, "width": 75},
            {"text": "Applied At", "stretch": False, "width": 130},
            {"text": "Error", "stretch": True, "width": 150},
        ]
        self.history_table_parent = bot_pane
        self.history_table = Tableview(
            bot_pane, coldata=self._history_col_defs, rowdata=[], paginated=True, pagesize=10,
            searchable=True, bootstyle="info",
        )
        self.history_table.pack(fill=BOTH, expand=True)

        self.apply_log = ScrolledText(frame, height=4, autohide=True)
        self.apply_log.pack(fill=X, pady=(5, 0))

    # ── Jobs Tab ────────────────────────────────────────────────────────

    def _build_jobs_tab(self):
        frame = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(frame, text="  Jobs  ")
        self.jobs_frame = frame  # Store reference for table rebuild

        top = ttk.Frame(frame)
        top.pack(fill=X, pady=(0, 10))
        ttk.Label(top, text="Discovered Jobs", font=("Segoe UI", 13, "bold")).pack(side=LEFT)
        ttk.Button(top, text="Clear All Jobs", bootstyle="danger-outline", command=self._clear_all_jobs,
                   width=14).pack(side=RIGHT, padx=5)
        ttk.Button(top, text="Refresh", bootstyle="info-outline", command=self._refresh_jobs_table,
                   width=10).pack(side=RIGHT)

        # Filter
        filt = ttk.Frame(frame)
        filt.pack(fill=X, pady=(0, 5))
        ttk.Label(filt, text="Filter:").pack(side=LEFT)
        self.jobs_filter = ttk.StringVar(value="all")
        for val, label in [("all", "All"), ("scored", "Scored"), ("unscored", "Unscored"), ("high", "High Match (>0.7)")]:
            ttk.Radiobutton(filt, text=label, variable=self.jobs_filter, value=val,
                            command=self._refresh_jobs_table).pack(side=LEFT, padx=8)

        # Table
        self._jobs_col_defs = [
            {"text": "ID", "stretch": False, "width": 50},
            {"text": "Title", "stretch": True, "width": 200},
            {"text": "Company", "stretch": True, "width": 150},
            {"text": "Location", "stretch": False, "width": 120},
            {"text": "Platform", "stretch": False, "width": 80},
            {"text": "Score", "stretch": False, "width": 60},
            {"text": "ATS", "stretch": False, "width": 80},
        ]
        self.jobs_table = Tableview(
            frame, coldata=self._jobs_col_defs, rowdata=[], paginated=True, pagesize=25,
            searchable=True, bootstyle="primary",
        )
        self.jobs_table.pack(fill=BOTH, expand=True)

    # ── Log Tab ─────────────────────────────────────────────────────────

    def _build_log_tab(self):
        frame = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(frame, text="  Log  ")

        top = ttk.Frame(frame)
        top.pack(fill=X, pady=(0, 5))
        ttk.Label(top, text="Activity Log", font=("Segoe UI", 13, "bold")).pack(side=LEFT)
        ttk.Button(top, text="Clear", bootstyle="danger-outline", command=self._clear_log,
                   width=8).pack(side=RIGHT)

        self.log_text = ScrolledText(frame, height=30, autohide=True)
        self.log_text.pack(fill=BOTH, expand=True)

    # ── Config I/O ──────────────────────────────────────────────────────

    def _try_load_config(self):
        """Try to load config and populate UI fields."""
        try:
            if not CONFIG_PATH.exists():
                if EXAMPLE_CONFIG_PATH.exists():
                    shutil.copy(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
                    self._log("INFO", "Created config.yaml from example template. Please fill in your details.")
                else:
                    self._log("WARNING", "No config file found. Please configure settings.")
                    self.notebook.select(1)  # Switch to Config tab
                    return

            # Load without validation first so UI always populates
            self.config = load_config(validate=False)
            self.db = Database()
            self._populate_config_ui()
            self._refresh_stats()
            self._refresh_match_table()
            self._refresh_jobs_table()
            self._refresh_apply_info()

            # Now check if required fields are filled
            missing = []
            if not self.config.personal_info.full_name:
                missing.append("Full Name")
            if not self.config.personal_info.email:
                missing.append("Email")
            if not self.config.job_preferences.titles:
                missing.append("Job Titles")

            if missing:
                self.status_label.config(text="Setup required", bootstyle="warning")
                self._log("WARNING", f"Please fill in: {', '.join(missing)}. Then click Save Config.")
                self.notebook.select(1)  # Switch to Config tab
            else:
                self.status_label.config(text="Config loaded", bootstyle="success")
                self._log("INFO", "Configuration loaded successfully.")
        except Exception as e:
            self.status_label.config(text="Config error", bootstyle="danger")
            self._log("ERROR", f"Failed to load config: {e}")

    def _populate_config_ui(self):
        if not self.config:
            return
        p = self.config.personal_info
        for key in ["full_name", "email", "phone", "linkedin_url", "website", "current_company",
                     "years_experience", "notice_period", "password", "address", "city", "state", "zip_code"]:
            self.cfg_vars[f"personal_{key}"].set(getattr(p, key, ""))

        self.titles_text.delete("1.0", "end")
        self.titles_text.insert("1.0", "\n".join(self.config.job_preferences.titles))

        self.cfg_vars["locations"].set(", ".join(self.config.job_preferences.locations))
        self.cfg_vars["min_score"].set(str(self.config.job_preferences.min_match_score))
        min_sal = self.config.job_preferences.min_salary
        self.cfg_vars["min_salary"].set(str(min_sal) if min_sal else "")
        self.cfg_vars["ollama_model"].set(self.config.ollama.model)
        self.cfg_vars["ollama_match_model"].set(self.config.ollama.match_model)
        self.cfg_vars["ollama_url"].set(self.config.ollama.base_url)
        self.cfg_vars["max_daily"].set(str(self.config.application.max_daily_applications))
        delays = self.config.application.delay_between_applications
        self.cfg_vars["delay_min"].set(str(delays[0]))
        self.cfg_vars["delay_max"].set(str(delays[1]))
        self.cfg_vars["cover_letter"].set(self.config.application.generate_cover_letter)
        self.cfg_vars["headless"].set(self.config.browser.headless)
        self.cfg_vars["resume_path"].set(self.config.application.resume_path)

        # CDP settings
        cdp = self.config.cdp
        self.cfg_vars["cdp_chrome_path"].set(cdp.chrome_path or "")
        self.cfg_vars["cdp_base_port"].set(str(cdp.base_port))
        self.cfg_vars["cdp_profile_dir"].set(cdp.profile_dir)

        # CAPTCHA settings
        cap = self.config.captcha
        self.cfg_vars["captcha_enabled"].set(cap.enabled)
        self.cfg_vars["captcha_provider"].set(cap.provider)
        self.cfg_vars["captcha_api_key"].set(cap.api_key)

        # Email settings
        email_cfg = self.config.email
        self.cfg_vars["email_enabled"].set(email_cfg.enabled)
        self.cfg_vars["email_address"].set(email_cfg.email)
        self.cfg_vars["email_app_password"].set(email_cfg.app_password)

        # API scraper settings
        self.cfg_vars["adzuna_app_id"].set(self.config.adzuna.app_id)
        self.cfg_vars["adzuna_app_key"].set(self.config.adzuna.app_key)
        self.cfg_vars["careerjet_affid"].set(self.config.careerjet.affid)

        # Populate scrape filters from config
        prefs = self.config.job_preferences
        self.scrape_location_var.set(", ".join(prefs.locations))
        self.scrape_country_var.set(prefs.country_indeed or "UK")
        self.scrape_remote_var.set(prefs.is_remote)
        self.scrape_job_type_var.set(prefs.job_type or "")
        self.scrape_distance_var.set(str(prefs.distance or 50))
        self.scrape_results_var.set(str(prefs.results_wanted or 50))
        self.scrape_hours_var.set(str(prefs.hours_old or 72))

    def _save_config(self):
        """Save current UI values to config.yaml."""
        try:
            titles_raw = self.titles_text.get("1.0", "end").strip()
            titles = [t.strip() for t in titles_raw.splitlines() if t.strip()]
            locations = [l.strip() for l in self.cfg_vars["locations"].get().split(",") if l.strip()]

            data = {
                "job_preferences": {
                    "titles": titles,
                    "locations": locations,
                    "min_match_score": float(self.cfg_vars["min_score"].get()),
                    "min_salary": int(self.cfg_vars["min_salary"].get()) if self.cfg_vars["min_salary"].get().strip() else None,
                },
                "personal_info": {
                    key: self.cfg_vars[f"personal_{key}"].get()
                    for key in ["full_name", "email", "phone", "linkedin_url", "website", "current_company",
                                "years_experience", "notice_period", "password", "address", "city", "state", "zip_code"]
                },
                "ollama": {
                    "model": self.cfg_vars["ollama_model"].get(),
                    "match_model": self.cfg_vars["ollama_match_model"].get(),
                    "base_url": self.cfg_vars["ollama_url"].get(),
                },
                "browser": {
                    "headless": self.cfg_vars["headless"].get(),
                    "slow_mo": 500,
                    "timeout": 30000,
                },
                "application": {
                    "max_daily_applications": int(self.cfg_vars["max_daily"].get()),
                    "delay_between_applications": [
                        int(self.cfg_vars["delay_min"].get()),
                        int(self.cfg_vars["delay_max"].get()),
                    ],
                    "generate_cover_letter": self.cfg_vars["cover_letter"].get(),
                    "resume_path": self.cfg_vars["resume_path"].get(),
                },
                "logging": {"level": "INFO", "file": "bot.log"},
                "cdp": {
                    "chrome_path": self.cfg_vars["cdp_chrome_path"].get(),
                    "base_port": int(self.cfg_vars["cdp_base_port"].get()),
                    "profile_dir": self.cfg_vars["cdp_profile_dir"].get(),
                },
                "captcha": {
                    "provider": self.cfg_vars["captcha_provider"].get(),
                    "api_key": self.cfg_vars["captcha_api_key"].get(),
                    "enabled": self.cfg_vars["captcha_enabled"].get(),
                },
                "email": {
                    "imap_host": "imap.gmail.com",
                    "email": self.cfg_vars["email_address"].get(),
                    "app_password": self.cfg_vars["email_app_password"].get(),
                    "enabled": self.cfg_vars["email_enabled"].get(),
                },
                "adzuna": {
                    "app_id": self.cfg_vars["adzuna_app_id"].get(),
                    "app_key": self.cfg_vars["adzuna_app_key"].get(),
                },
                "careerjet": {
                    "affid": self.cfg_vars["careerjet_affid"].get(),
                },
            }

            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

            self._log("INFO", "Config saved to config/config.yaml")
            self._try_load_config()
            messagebox.showinfo("Saved", "Configuration saved successfully.")
        except Exception as e:
            self._log("ERROR", f"Failed to save config: {e}")
            messagebox.showerror("Error", f"Failed to save: {e}")

    def _browse_resume(self):
        path = filedialog.askopenfilename(
            title="Select Resume PDF",
            filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")],
        )
        if path:
            # Copy to resume/ directory
            dest = Path("resume")
            dest.mkdir(exist_ok=True)
            dest_file = dest / Path(path).name
            if str(Path(path).resolve()) != str(dest_file.resolve()):
                shutil.copy2(path, dest_file)
            self.cfg_vars["resume_path"].set(str(dest_file))
            self._log("INFO", f"Resume selected: {dest_file}")
            # Auto-parse after selecting
            self._parse_and_preview_resume()

    def _parse_and_preview_resume(self):
        """Parse the resume PDF and send to Ollama to extract structured info."""
        resume_path = self.cfg_vars["resume_path"].get()
        if not resume_path or not Path(resume_path).exists():
            messagebox.showwarning("No Resume", "Please select a resume PDF first.")
            return

        self.resume_status_var.set("Parsing PDF...")
        self.root.update_idletasks()

        model_var = self.cfg_vars.get("ollama_model")
        model_name = model_var.get() if model_var else "kimi-k2.5:cloud"
        url_var = self.cfg_vars.get("ollama_url")
        base_url = url_var.get() if url_var else "http://localhost:11434"
        # Vision model for OCR of image-based PDFs
        vision_model = self.config.ollama.vision_model if self.config else "kimi-k2.5:cloud"

        # Run parsing + AI extraction in a background thread (both may be slow)
        def _extract():
            try:
                from src.resume_parser import parse_resume
                self.root.after(0, lambda: self.resume_status_var.set("Parsing PDF (may use AI for image PDFs)..."))
                raw_text = parse_resume(
                    resume_path,
                    ollama_model=vision_model,
                    ollama_url=base_url,
                )
                self._resume_raw_text = raw_text
                text_len = len(raw_text)
                self.root.after(0, lambda: self._log("INFO", f"PDF raw text extracted: {text_len} chars"))

                if not raw_text.strip():
                    self.root.after(0, lambda: self._resume_extract_failed("PDF text extraction returned empty — is it a scanned/image PDF?"))
                    return

                self.root.after(0, lambda: self.resume_status_var.set("Extracting structured info with AI..."))

                import ollama as ollama_client
                client = ollama_client.Client(host=base_url)

                prompt = f"""Extract the following information from this resume. Return it in this exact format:

Full Name: <name>
Email: <email>
Phone: <phone>
LinkedIn: <linkedin url>
Website: <website or portfolio url>
Current Company: <most recent company>
Years of Experience: <estimated total years>
Key Skills: <comma-separated list of top skills>
Summary: <2-3 sentence professional summary>

If a field is not found, write "Not found".

Resume text:
{raw_text[:4000]}"""

                response = client.chat(model=model_name, messages=[
                    {"role": "system", "content": "You extract structured information from resumes. Be accurate and concise. Only return the requested format, nothing else."},
                    {"role": "user", "content": prompt},
                ])
                result = response.message.content
                self.root.after(0, lambda: self._show_resume_preview(result))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: self._resume_extract_failed(err_msg))

        threading.Thread(target=_extract, daemon=True).start()

    def _show_resume_preview(self, extracted_info: str):
        """Display extracted resume info for user review."""
        self.resume_preview.delete("1.0", "end")
        self.resume_preview.insert("1.0", extracted_info)
        self.resume_status_var.set("Review the info below, edit if needed, then click Auto-fill")
        self._log("INFO", "Resume parsed and analyzed. Please review the extracted info.")

    def _resume_extract_failed(self, error: str):
        """Show raw text if AI extraction fails."""
        self.resume_status_var.set("AI extraction failed - showing raw text")
        self.resume_preview.delete("1.0", "end")
        self.resume_preview.insert("1.0", getattr(self, "_resume_raw_text", "No text available"))
        self._log("WARNING", f"AI extraction failed: {error}. Showing raw resume text instead.")

    def _autofill_from_resume(self):
        """Parse the preview text and auto-fill config fields."""
        text = self.resume_preview.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("No Data", "Parse the resume first before auto-filling.")
            return

        field_map = {
            "Full Name": "personal_full_name",
            "Email": "personal_email",
            "Phone": "personal_phone",
            "LinkedIn": "personal_linkedin_url",
            "Website": "personal_website",
            "Current Company": "personal_current_company",
            "Years of Experience": "personal_years_experience",
        }

        filled = []
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key in field_map and value and value.lower() != "not found":
                var_name = field_map[key]
                if var_name in self.cfg_vars:
                    self.cfg_vars[var_name].set(value)
                    filled.append(key)

        if filled:
            self._log("INFO", f"Auto-filled: {', '.join(filled)}")
            messagebox.showinfo("Auto-fill Complete", f"Filled {len(filled)} fields:\n{', '.join(filled)}\n\nPlease review and click Save Config.")
        else:
            messagebox.showwarning("No Fields Matched", "Could not extract fields. Please fill them in manually.")

    # ── Pipeline Actions ────────────────────────────────────────────────

    def _ensure_ready(self) -> bool:
        if not self.config or not self.db:
            messagebox.showwarning("Not Ready", "Please load a valid config first.")
            return False
        # Validate required fields before running
        missing = []
        if not self.config.personal_info.full_name:
            missing.append("Full Name")
        if not self.config.personal_info.email:
            missing.append("Email")
        if not self.config.job_preferences.titles:
            missing.append("Job Titles")
        if missing:
            messagebox.showwarning("Setup Required", f"Please fill in: {', '.join(missing)}\n\nGo to the Config tab and click Save Config.")
            self.notebook.select(1)
            return False
        if self.runner.running:
            messagebox.showinfo("Busy", "A task is already running. Please wait.")
            return False
        return True

    def _run_scrape(self):
        if not self._ensure_ready():
            return
        from src.main import run_scrape
        platforms = [k for k, v in self.scrape_platform_vars.items() if v.get()]
        if not platforms:
            from tkinter import messagebox
            messagebox.showwarning("No Platforms", "Please select at least one platform.")
            return

        # Apply scrape filter overrides to config
        loc = self.scrape_location_var.get().strip()
        if loc:
            self.config.job_preferences.locations = [l.strip() for l in loc.split(",") if l.strip()]
        country = self.scrape_country_var.get().strip() or "UK"
        self.config.job_preferences.country_indeed = country
        # Auto-add country to locations list so post-scrape filter uses it
        if country and country.lower() not in [l.lower() for l in self.config.job_preferences.locations]:
            self.config.job_preferences.locations.append(country)
        self.config.job_preferences.is_remote = self.scrape_remote_var.get()
        self.config.job_preferences.job_type = self.scrape_job_type_var.get().strip()
        try:
            self.config.job_preferences.distance = int(self.scrape_distance_var.get())
        except ValueError:
            self.config.job_preferences.distance = 50
        try:
            self.config.job_preferences.results_wanted = int(self.scrape_results_var.get())
        except ValueError:
            self.config.job_preferences.results_wanted = 50
        try:
            self.config.job_preferences.hours_old = int(self.scrape_hours_var.get())
        except ValueError:
            self.config.job_preferences.hours_old = 72

        filters_desc = []
        if loc:
            filters_desc.append(f"location={loc}")
        if self.config.job_preferences.is_remote:
            filters_desc.append("remote")
        if self.config.job_preferences.job_type:
            filters_desc.append(f"type={self.config.job_preferences.job_type}")
        extra = f" [{', '.join(filters_desc)}]" if filters_desc else ""
        platform_desc = ", ".join(platforms) if len(platforms) < 8 else "all"
        self._log("INFO", f"Starting scrape ({platform_desc}){extra}...")

        self._set_running(True)
        self.runner.run(run_scrape, self.config, self.db, platforms,
                        on_done=lambda: self.root.after(0, self._on_task_done))

    def _run_match(self):
        if not self._ensure_ready():
            return
        from src.main import run_match
        try:
            batch_size = int(self.match_batch_var.get())
        except ValueError:
            batch_size = 0
        batch_msg = f" (batch of {batch_size})" if batch_size > 0 else " (all unscored)"
        self._log("INFO", f"Starting AI matching{batch_msg}...")
        self._set_running(True)
        self.runner.run(run_match, self.config, self.db, self.runner.cancel_event, batch_size,
                        on_done=lambda: self.root.after(0, self._on_task_done))

    def _run_apply(self):
        if not self._ensure_ready():
            return
        from src.main import run_apply
        dry = self.dry_run_var.get()
        # Apply workers setting from GUI
        try:
            self.config.application.num_workers = max(1, int(self.workers_var.get()))
        except (ValueError, AttributeError):
            self.config.application.num_workers = 1
        workers = self.config.application.num_workers
        # Force browser visible during apply so user can watch
        self.config.browser.headless = False
        self._log("INFO", f"Starting auto-apply ({'DRY RUN' if dry else 'LIVE'}) with {workers} worker(s) — browser window(s) will open...")
        self._set_running(True)
        self.runner.run(run_apply, self.config, self.db, dry,
                        on_done=lambda: self.root.after(0, self._on_task_done))

    def _run_pipeline(self):
        if not self._ensure_ready():
            return
        from src.main import run_full_pipeline
        self._log("INFO", "Starting full pipeline...")
        self._set_running(True)
        self.runner.run(run_full_pipeline, self.config, self.db, False,
                        on_done=lambda: self.root.after(0, self._on_task_done))

    def _run_pipeline_dry(self):
        if not self._ensure_ready():
            return
        from src.main import run_full_pipeline
        self._log("INFO", "Starting full pipeline (DRY RUN)...")
        self._set_running(True)
        self.runner.run(run_full_pipeline, self.config, self.db, True,
                        on_done=lambda: self.root.after(0, self._on_task_done))

    def _set_running(self, running: bool):
        if running:
            self.status_label.config(text="Running...", bootstyle="warning")
            self.stop_btn.pack(side=RIGHT, padx=5)
        else:
            self.status_label.config(text="Ready", bootstyle="success")
            self.stop_btn.pack_forget()

    def _on_task_done(self):
        self._set_running(False)
        self._refresh_stats()
        self._refresh_jobs_table()
        self._refresh_match_table()
        self._refresh_apply_info()

    # ── Stats / Refresh ─────────────────────────────────────────────────

    def _refresh_stats(self):
        if not self.db:
            return
        try:
            stats = self.db.get_stats()
            for key, var in self.stat_vars.items():
                var.set(str(stats.get(key, 0)))

            # Dashboard jobs table
            all_jobs = self.db.get_all_jobs()
            rows = []
            for j in all_jobs:
                score = f"{j['match_score']:.2f}" if j["match_score"] is not None else "\u2014"
                rows.append((
                    j["title"] or "",
                    j["company"] or "",
                    j["location"] or "",
                    j["platform"] or "",
                    score,
                    j["ats_type"] or "\u2014",
                ))
            self.dash_jobs_table.destroy()
            self.dash_jobs_table = Tableview(
                self.dash_jobs_frame, coldata=self._dash_jobs_col_defs, rowdata=rows,
                paginated=True, pagesize=20, searchable=True, bootstyle="info",
            )
            self.dash_jobs_table.pack(fill=BOTH, expand=True)
        except Exception as e:
            self._log("ERROR", f"Failed to refresh stats: {e}")

    def _refresh_match_info(self):
        if not self.db:
            return
        unscored = len(self.db.get_unscored_jobs())
        stats = self.db.get_stats()
        self.match_info_var.set(
            f"{unscored} unscored jobs | {stats['scored_jobs']} already scored"
        )
        self._refresh_match_table()

    def _refresh_match_table(self):
        """Populate the match tab table with scored jobs, sorted by score descending."""
        if not self.db:
            return
        try:
            all_jobs = self.db.get_all_jobs()
            scored = [j for j in all_jobs if j["match_score"] is not None]
            scored.sort(key=lambda j: j["match_score"], reverse=True)

            rows = []
            for j in scored:
                rows.append((
                    f"{j['match_score']:.2f}",
                    j["title"] or "",
                    j["company"] or "",
                    j["location"] or "",
                    j["platform"] or "",
                    (j.get("match_reasoning") or "")[:120],
                ))

            self.match_table.destroy()
            self.match_table = Tableview(
                self.match_frame, coldata=self._match_col_defs, rowdata=rows,
                paginated=True, pagesize=25, searchable=True, bootstyle="primary",
            )
            self.match_table.pack(fill=BOTH, expand=True, before=self.match_log)
        except Exception as e:
            self._log("ERROR", f"Failed to refresh match table: {e}")

    def _refresh_apply_info(self):
        if not self.db or not self.config:
            return
        matched = self.db.get_matched_jobs(self.config.job_preferences.min_match_score)
        daily = self.db.get_daily_application_count()
        total_apps = len(self.db.get_applications())
        self.apply_info_var.set(
            f"{len(matched)} jobs ready | {daily} applied today | {total_apps} total applications"
        )
        self._refresh_apply_table(matched)
        self._refresh_history_table()

    def _refresh_apply_table(self, matched_jobs: list[dict] | None = None):
        """Populate the apply tab table with matched jobs ready to apply."""
        if not self.db or not self.config:
            return
        try:
            if matched_jobs is None:
                matched_jobs = self.db.get_matched_jobs(self.config.job_preferences.min_match_score)

            rows = []
            for j in matched_jobs:
                has_tailored = "Yes" if j.get("tailored_resume") else "No"
                rows.append((
                    f"{j['match_score']:.2f}",
                    j["title"] or "",
                    j["company"] or "",
                    j["location"] or "",
                    j["platform"] or "",
                    has_tailored,
                ))

            self.apply_table.destroy()
            self.apply_table = Tableview(
                self.apply_table_parent, coldata=self._apply_col_defs, rowdata=rows,
                paginated=False, searchable=True, bootstyle="success", height=15,
            )
            self.apply_table.pack(fill=BOTH, expand=True)
        except Exception as e:
            self._log("ERROR", f"Failed to refresh apply table: {e}")

    def _refresh_history_table(self):
        """Populate the application history table with past applications."""
        if not self.db:
            return
        try:
            apps = self.db.get_applications()
            rows = []
            for a in apps:
                status = a["status"]
                # Color-code status text
                status_display = {
                    "submitted": "Submitted",
                    "failed": "FAILED",
                    "pending": "Pending",
                    "skipped": "Skipped (dry)",
                }.get(status, status)

                rows.append((
                    a.get("title", "N/A"),
                    a.get("company", "N/A"),
                    status_display,
                    a.get("ats_type_used", "") or "",
                    a.get("applied_at", "") or "",
                    (a.get("error_message", "") or "")[:80],
                ))

            self.history_table.destroy()
            self.history_table = Tableview(
                self.history_table_parent, coldata=self._history_col_defs, rowdata=rows,
                paginated=True, pagesize=10, searchable=True, bootstyle="info",
            )
            self.history_table.pack(fill=BOTH, expand=True)
        except Exception as e:
            self._log("ERROR", f"Failed to refresh history table: {e}")

    def _refresh_jobs_table(self):
        if not self.db:
            return
        try:
            filt = self.jobs_filter.get()
            all_jobs = self.db.get_all_jobs()

            if filt == "scored":
                all_jobs = [j for j in all_jobs if j["match_score"] is not None]
            elif filt == "unscored":
                all_jobs = [j for j in all_jobs if j["match_score"] is None]
            elif filt == "high":
                all_jobs = [j for j in all_jobs if j["match_score"] is not None and j["match_score"] > 0.7]

            rows = []
            for j in all_jobs:
                score = f"{j['match_score']:.2f}" if j["match_score"] is not None else "\u2014"
                rows.append((
                    j["id"],
                    j["title"] or "",
                    j["company"] or "",
                    j["location"] or "",
                    j["platform"] or "",
                    score,
                    j["ats_type"] or "\u2014",
                ))

            # Rebuild the Tableview widget entirely for a clean refresh
            self.jobs_table.destroy()
            self.jobs_table = Tableview(
                self.jobs_frame, coldata=self._jobs_col_defs, rowdata=rows,
                paginated=True, pagesize=25, searchable=True, bootstyle="primary",
            )
            self.jobs_table.pack(fill=BOTH, expand=True)
        except Exception as e:
            self._log("ERROR", f"Failed to refresh jobs table: {e}")

    def _clear_all_jobs(self):
        """Delete all jobs, applications, and search history."""
        if not self.db:
            return
        if not messagebox.askyesno(
            "Confirm Clear",
            "Delete ALL jobs, applications, and search history?\n\nThis cannot be undone."
        ):
            return
        self.db.clear_all_jobs()
        self._refresh_jobs_table()
        self._refresh_stats()
        self._log("INFO", "All jobs and applications cleared.")

    def _stop_task(self):
        """Stop the currently running background task."""
        if self.runner.running:
            self.runner.cancel()
            self._log("INFO", "Stop requested — the current job will finish, then stop.")
        else:
            self._log("WARNING", "No task is currently running.")

    # ── Logging ─────────────────────────────────────────────────────────

    def _log(self, level: str, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {level}: {message}\n"

        self.log_text.text.insert("end", line)
        self.log_text.text.see("end")

        # Also append to the active tab's log if applicable
        active_tab = self.notebook.index(self.notebook.select())
        tab_logs = {2: self.scrape_log, 3: self.match_log, 4: self.apply_log}
        if active_tab in tab_logs:
            tab_logs[active_tab].text.insert("end", line)
            tab_logs[active_tab].text.see("end")

    def _clear_log(self):
        self.log_text.text.delete("1.0", "end")

    def _poll_log_queue(self):
        """Poll the log queue for messages from background threads."""
        while not self.log_queue.empty():
            try:
                level, msg = self.log_queue.get_nowait()
                self._log(level, msg)
            except queue.Empty:
                break
        self.root.after(200, self._poll_log_queue)

    # ── Run ─────────────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


def main():
    setup_logging("INFO")
    app = AppBotGUI()
    app.run()


if __name__ == "__main__":
    main()
