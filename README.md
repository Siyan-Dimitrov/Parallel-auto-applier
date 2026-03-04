# Parallel Auto-Applier

An autonomous job application bot that scrapes job listings from multiple platforms, scores them against your profile using AI, and automatically fills and submits applications using browser automation.

## Features

- **Multi-platform scraping** — LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Hiring.cafe, Adzuna, Careerjet, Workday, and SmartExtract
- **AI-powered job matching** — Uses Ollama LLMs to score jobs against your resume and preferences
- **Autonomous form filling** — Claude Code CLI + Playwright MCP navigates application forms, fills fields, uploads resumes, and submits
- **Parallel workers** — Multiple Chrome CDP instances for concurrent applications
- **Email verification** — Gmail IMAP integration for automatic email verification during signup flows
- **CAPTCHA support** — Optional CapSolver integration for CAPTCHA challenges
- **Location filtering** — Smart country alias matching (UK/United Kingdom/England/etc.)
- **Salary filtering** — Filter jobs by minimum salary requirements
- **Resume tailoring** — AI-powered resume customization per job
- **GUI** — Tkinter-based GUI for configuration and monitoring

## Architecture

```
Scrape → Score → Apply (parallel)
  │        │        │
  │        │        ├── Claude Code CLI
  │        │        ├── Playwright MCP (browser automation)
  │        │        └── Chrome CDP (persistent sessions)
  │        │
  │        └── Ollama LLM (match scoring)
  │
  ├── JobSpy (LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google)
  ├── Hiring.cafe (API)
  ├── Adzuna (API)
  ├── Careerjet (API)
  ├── Workday (direct)
  └── SmartExtract (browser)
```

## Prerequisites

- **Python 3.11+**
- **Ollama** — running locally with models pulled (e.g. `llama3.2:3b`, `qwen2.5:14b`)
- **Claude Code CLI** — for autonomous application filling
- **Node.js / npx** — for Playwright MCP server
- **Chrome** — for CDP browser automation

## Setup

1. Clone the repo and create a virtual environment:
   ```bash
   git clone https://github.com/Siyan-Dimitrov/Parallel-auto-applier.git
   cd Parallel-auto-applier
   python -m venv venv
   venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```

2. Copy the example config and fill in your details:
   ```bash
   cp config/config.example.yaml config/config.yaml
   ```

3. Edit `config/config.yaml` with your personal info, API keys, and preferences.

4. Edit `config/profile.json` with your skills, work authorization, and compensation details.

5. Place your resume PDF in `resume/resume.pdf` (or update the path in config).

6. Launch the GUI:
   ```bash
   python gui.py
   ```
   Or use the batch launcher: `launch_gui.bat`

## Configuration

### `config/config.yaml`
Main configuration file (gitignored — never committed). Contains:
- Personal info (name, email, phone, address)
- Job preferences (titles, locations, match score threshold)
- Ollama model settings
- Browser settings
- API keys (Adzuna, Careerjet, CAPTCHA solver)
- Email verification settings (Gmail IMAP)

### `config/profile.json`
Skills, resume facts, work authorization, compensation, and EEO data.

### `config/employers.yaml`
Workday employer configurations for direct scraping.

### `config/sites.yaml`
Additional career page URLs to monitor.

## How It Works

1. **Scrape** — Collects job listings from selected platforms based on your title/location preferences
2. **Score** — Each job is scored (0-1) against your resume using an Ollama LLM. Jobs below `min_match_score` are filtered out.
3. **Apply** — For each qualifying job, launches Claude Code CLI with Playwright MCP connected to a Chrome CDP instance. The AI agent navigates the application form, fills fields from your profile, uploads your resume, and submits.
4. **Track** — All applications are logged in a local SQLite database (`data/bot.db`) with status tracking and error classification.

## License

MIT
