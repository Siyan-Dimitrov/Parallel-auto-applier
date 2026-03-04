"""Prompt builder for the Claude Code applicator.

Generates a comprehensive system prompt that instructs the AI agent
how to navigate to a job application URL and fill out the form
autonomously using Playwright MCP tools.
"""
from __future__ import annotations

from src.config import (
    PersonalInfo,
    ProfileConfig,
    JobPreferences,
    SkillsBoundary,
    ResumeFacts,
    WorkAuthorization,
    Compensation,
    EEOVoluntary,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_prompt(
    apply_url: str,
    personal_info: PersonalInfo,
    profile: ProfileConfig,
    job_preferences: JobPreferences,
    resume_path: str,
    resume_text: str = "",
    cover_letter: str | None = None,
    job_title: str = "",
    job_company: str = "",
    job_location: str = "",
    job_description: str = "",
    tailored_resume_text: str = "",
    dry_run: bool = False,
    captcha_enabled: bool = False,
    captcha_api_key: str = "",
    verification_code: str = "",
    verification_link: str = "",
) -> str:
    """Build the full system prompt for the Claude Code applicator.

    Each logical section is produced by a helper function and the results
    are joined into a single string that serves as the agent prompt.
    """
    sections: list[str] = [
        _section_role(),
        _section_applicant_profile(personal_info),
        _section_work_authorization(profile.work_authorization),
        _section_compensation(profile.compensation),
        _section_job_context(
            job_title, job_company, job_location, job_description, job_preferences,
        ),
        _section_resume(
            resume_path, resume_text, tailored_resume_text, profile.resume_facts,
        ),
        _section_skills(profile.skills_boundary),
        _section_eeo(profile.eeo_voluntary),
    ]

    if cover_letter:
        sections.append(_section_cover_letter(cover_letter))

    if verification_code or verification_link:
        sections.append(
            _section_email_verification(verification_code, verification_link),
        )

    sections.append(
        _section_application_flow(apply_url, dry_run, personal_info, has_verification=bool(verification_code or verification_link)),
    )
    sections.append(
        _section_captcha(captcha_enabled, captcha_api_key),
    )
    sections.append(_section_result_codes())
    sections.append(_section_hard_rules())

    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# 1. ROLE
# ---------------------------------------------------------------------------

def _section_role() -> str:
    return (
        "## ROLE\n\n"
        "You are an expert job application assistant. Your task is to navigate "
        "to the provided URL and complete the job application form. You have "
        "access to a full browser via Playwright MCP tools. You will fill every "
        "field accurately, upload the resume, and submit the application.\n\n"
        "TOOL RULES: ONLY use high-level MCP tools (browser_snapshot, browser_click, "
        "browser_type, browser_fill_form, browser_select_option, browser_file_upload, "
        "browser_navigate, browser_tabs). NEVER use browser_run_code or browser_evaluate.\n\n"
        "IMPORTANT: When you finish (success or failure), you MUST output a "
        "RESULT code (e.g. RESULT:APPLIED or RESULT:FAILED:reason). "
        "See the RESULT CODES section below for the full list."
    )


# ---------------------------------------------------------------------------
# 2. APPLICANT PROFILE
# ---------------------------------------------------------------------------

def _section_applicant_profile(p: PersonalInfo) -> str:
    first_name, last_name = _split_name(p.full_name)

    lines = [
        "## APPLICANT PROFILE\n",
        f"- Full Name: {p.full_name}",
        f"- First Name: {first_name}",
        f"- Last Name: {last_name}",
        f"- Email: {p.email}",
        f"- Phone: {p.phone}",
    ]

    if p.linkedin_url:
        lines.append(f"- LinkedIn: {p.linkedin_url}")
    if p.website and p.website not in ("", "N/A"):
        lines.append(f"- Website / Portfolio: {p.website}")

    # Address fields (may not exist on older PersonalInfo versions)
    address = getattr(p, "address", "")
    city = getattr(p, "city", "")
    state = getattr(p, "state", "")
    zip_code = getattr(p, "zip_code", "")
    if address or city or state or zip_code:
        lines.append("")
        lines.append("### Address")
        if address:
            lines.append(f"- Street Address: {address}")
        if city:
            lines.append(f"- City: {city}")
        if state:
            lines.append(f"- State / Province: {state}")
        if zip_code:
            lines.append(f"- Zip / Postal Code: {zip_code}")

    # Password for site account creation
    password = getattr(p, "password", "")
    if password:
        lines.append("")
        lines.append(f"- Password: {password}")
        lines.append(
            "  (Use this password ONLY for creating accounts on job application "
            "sites. Never share it elsewhere.)"
        )

    if p.current_company:
        lines.append(f"- Current Company: {p.current_company}")
    if p.years_experience:
        lines.append(f"- Years of Experience: {p.years_experience}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2b. WORK AUTHORIZATION
# ---------------------------------------------------------------------------

def _section_work_authorization(wa: WorkAuthorization) -> str:
    return "\n".join([
        "## WORK AUTHORIZATION\n",
        f"- Legally authorized to work: {'Yes' if wa.legally_authorized else 'No'}",
        f"- Require visa sponsorship: {'Yes' if wa.require_sponsorship else 'No'}",
    ])


# ---------------------------------------------------------------------------
# 2c. COMPENSATION
# ---------------------------------------------------------------------------

def _section_compensation(comp: Compensation) -> str:
    lines = ["## COMPENSATION\n"]

    if comp.salary_expectation:
        lines.append(
            f"- Salary Expectation: {comp.salary_expectation} {comp.currency}"
        )
    if comp.range_min is not None and comp.range_max is not None:
        lines.append(
            f"- Acceptable Range: {comp.range_min:,} – {comp.range_max:,} {comp.currency}"
        )
    elif comp.range_min is not None:
        lines.append(
            f"- Minimum Acceptable Salary: {comp.range_min:,} {comp.currency}"
        )

    if not comp.salary_expectation and comp.range_min is None:
        lines.append("No compensation data provided. Leave salary fields blank if optional.")
        return "\n".join(lines)

    lines.append(
        "\nIf asked for a single number, use the salary expectation. If asked "
        "for a range, use the min and max. Never agree to a figure below the "
        "minimum."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. JOB CONTEXT
# ---------------------------------------------------------------------------

def _section_job_context(
    title: str,
    company: str,
    location: str,
    description: str,
    prefs: JobPreferences,
) -> str:
    lines = ["## JOB CONTEXT\n"]

    if title:
        lines.append(f"- Position Title: {title}")
    if company:
        lines.append(f"- Company: {company}")
    if location:
        lines.append(f"- Location: {location}")

    if description:
        truncated = description[:2000]
        if len(description) > 2000:
            truncated += "\n... (truncated)"
        lines.append(f"\n### Job Description\n{truncated}")

    if prefs.locations:
        lines.append(
            f"\n### Preferred Work Locations\n{', '.join(prefs.locations)}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. RESUME
# ---------------------------------------------------------------------------

def _section_resume(
    resume_path: str,
    resume_text: str,
    tailored_resume_text: str,
    facts: ResumeFacts,
) -> str:
    lines = [
        "## RESUME\n",
        f"### Resume File (for upload)\n{resume_path}",
    ]

    # Include resume text for answering screening questions
    effective_text = tailored_resume_text or resume_text
    if effective_text:
        lines.append(
            "\n### Resume Content (for filling text fields and answering questions)\n"
            "Use this text when you need to answer screening questions, fill "
            "\"tell us about yourself\" fields, or paste resume content into "
            "text areas.\n"
        )
        lines.append(effective_text.strip())

    # Resume facts — ground truth the agent must not deviate from
    has_facts = (
        facts.preserved_companies
        or facts.preserved_projects
        or facts.preserved_school
        or facts.real_metrics
    )
    if has_facts:
        lines.append("\n### Resume Facts (ground truth)")
        lines.append(
            "NEVER fabricate metrics or experiences not listed here. "
            "These are the verified facts from the applicant's real background."
        )
        if facts.preserved_companies:
            lines.append(
                f"- Companies: {', '.join(facts.preserved_companies)}"
            )
        if facts.preserved_projects:
            lines.append(
                f"- Projects: {', '.join(facts.preserved_projects)}"
            )
        if facts.preserved_school:
            lines.append(
                f"- Education: {', '.join(facts.preserved_school)}"
            )
        if facts.real_metrics:
            lines.append("- Real Metrics:")
            for metric in facts.real_metrics:
                lines.append(f"  - {metric}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. SKILLS
# ---------------------------------------------------------------------------

def _section_skills(skills: SkillsBoundary) -> str:
    all_skills = skills.all_skills()
    if not all_skills:
        return ""

    lines = ["## SKILLS\n"]

    if skills.languages:
        lines.append(f"- Languages: {', '.join(skills.languages)}")
    if skills.frameworks:
        lines.append(f"- Frameworks / Libraries: {', '.join(skills.frameworks)}")
    if skills.devops:
        lines.append(f"- DevOps / Cloud: {', '.join(skills.devops)}")
    if skills.databases:
        lines.append(f"- Databases: {', '.join(skills.databases)}")
    if skills.tools:
        lines.append(f"- Tools: {', '.join(skills.tools)}")

    lines.append(
        "\nOnly claim proficiency in the skills listed above. For any other "
        "skills mentioned in a form or screening question, say 'familiar' or "
        "'basic knowledge'. Never claim expertise you do not have."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. EEO / DEMOGRAPHICS
# ---------------------------------------------------------------------------

def _section_eeo(eeo: EEOVoluntary) -> str:
    has_data = eeo.gender or eeo.race_ethnicity or eeo.veteran_status or eeo.disability_status
    lines = ["## EEO / DEMOGRAPHICS (Voluntary Self-Identification)\n"]

    if has_data:
        if eeo.gender:
            lines.append(f"- Gender: {eeo.gender}")
        if eeo.race_ethnicity:
            lines.append(f"- Race / Ethnicity: {eeo.race_ethnicity}")
        if eeo.veteran_status:
            lines.append(f"- Veteran Status: {eeo.veteran_status}")
        if eeo.disability_status:
            lines.append(f"- Disability Status: {eeo.disability_status}")
    else:
        lines.append("No demographic data provided.")

    lines.append(
        "\nIf these fields are optional and no data is provided above, select "
        "'Decline to self-identify' or 'I do not wish to answer'. Never guess "
        "or fabricate demographic information."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. COVER LETTER
# ---------------------------------------------------------------------------

def _section_cover_letter(cover_letter: str) -> str:
    return (
        "## COVER LETTER\n\n"
        "If the application has a cover letter field (text area or upload), "
        "use the following cover letter:\n\n"
        f"{cover_letter.strip()}"
    )


# ---------------------------------------------------------------------------
# 8. STEP-BY-STEP APPLICATION FLOW
# ---------------------------------------------------------------------------

def _section_email_verification(code: str, link: str) -> str:
    """Prompt section for email verification retry."""
    lines = [
        "## EMAIL VERIFICATION\n",
        "The system has retrieved a verification email from the inbox. "
        "You are resuming after a previous attempt that required email verification. "
        "The browser is likely still on the verification page.\n",
    ]

    if code:
        lines.extend([
            f"**Verification Code: {code}**\n",
            "1. Take a browser_snapshot to see the current page state.",
            "2. Find the verification code input field.",
            "3. Enter the code above into the field.",
            "4. Click the Verify / Confirm / Submit button.",
            "5. Wait for the page to respond, then continue with the application flow.",
        ])

    if link:
        lines.extend([
            f"**Verification Link:** {link}\n",
            "1. Navigate to the verification link above using browser_navigate.",
            "2. Wait for the page to load and confirm verification succeeded.",
            "3. Navigate back to the application URL and continue the application flow.",
        ])

    return "\n".join(lines)


def _section_application_flow(apply_url: str, dry_run: bool, p: PersonalInfo | None = None, has_verification: bool = False) -> str:
    lines = [
        "## STEP-BY-STEP APPLICATION FLOW\n",
        "Follow these steps in order:\n",

        # ── STEP 0 ──
        "### STEP 0 — CLEAN UP BROWSER TABS\n",
        "- First, call browser_tabs with action \"list\" to see all open tabs.",
        "- Close ALL tabs EXCEPT the current one (tab index 0) by calling "
        "browser_tabs with action \"close\" for each extra tab, starting from "
        "the highest index down to 1.",
        "- This prevents the browser from becoming slow due to leftover tabs "
        "from previous sessions.\n",

        # ── STEP 1 ──
        "### STEP 1 — NAVIGATE\n",
    ]

    if has_verification:
        lines.extend([
            "- **VERIFICATION RETRY**: The browser already has a session open from "
            "a previous attempt. First take a browser_snapshot to see the current "
            "page state — you may already be on the verification page.",
            "- If you see a verification/code input page, handle the EMAIL "
            "VERIFICATION section above FIRST before proceeding.",
            "- If the page is not the verification page, navigate to the "
            f"application URL: {apply_url}\n",
        ])
    else:
        lines.extend([
            f"- Go to the application URL: {apply_url}",
            "- Use browser_navigate to open the page.",
            "- Take a snapshot with browser_snapshot to understand the page layout "
            "and identify what type of page you are on (job description, login, "
            "or application form).\n",
        ])

    lines.extend([

        # ── STEP 2 ──
        "### STEP 2 — FIND THE APPLICATION FORM\n",
        "- **LINKEDIN PAGES**: If you are on a LinkedIn job page (linkedin.com), "
        "look for an \"Apply\" button that redirects to the company's own website. "
        "If the ONLY option is \"Easy Apply\" (LinkedIn's built-in application), "
        "report RESULT:FAILED:sso_only — do NOT open the Easy Apply dialog.",
        "- If you see a job description page, look for an \"Apply\", \"Apply Now\", "
        "or \"Apply for this job\" button and click it.",
        "- If there are multiple apply options, ALWAYS choose \"Apply Manually\" "
        "or the direct apply option that leads to an external site.",
        "- NEVER use \"Apply with LinkedIn\", \"Apply with Google\", \"Easy Apply\", "
        "or any other SSO / third-party sign-in option.",
        "- Handle cookie banners and popups by dismissing or closing them "
        "(click \"Accept\", \"Close\", or the X button).",
        "- If the page shows \"This job is no longer available\" or similar, "
        "report RESULT:FAILED:expired\n",

        # ── STEP 3 ──
        "### STEP 3 — HANDLE LOGIN / SIGNUP\n",
        "- **FIRST** check what login/signup options the site offers. If the "
        "site ONLY provides \"Sign in with Google\", \"Continue with Google\", "
        "\"Sign in with LinkedIn\", \"Sign in with Microsoft\", or similar SSO "
        "buttons — with NO email/password signup form — immediately report "
        "RESULT:FAILED:sso_only. Do NOT click any SSO buttons.",
        "- If the site has an email/password signup form, create an account:\n"
        "  1. Use the email and password from the PERSONAL INFO section above.\n"
        "  2. Fill in name, email, password, and any required fields.\n"
        "  3. Click Register / Sign Up / Create Account.\n"
        "  4. If the site sends an email verification after signup, report "
        "RESULT:FAILED:email_verification_needed — the system will "
        "automatically fetch the verification code from the inbox and retry.",
        "- If the page requires login and you are NOT already logged in, "
        "try logging in with the email and password from the PERSONAL INFO "
        "section. If login fails (wrong credentials, no account exists), "
        "look for a \"Sign Up\" or \"Create Account\" link and create an account.",
        "- If you land on an EMAIL verification page (enter code, check your "
        "email, verify your email address), report "
        "RESULT:FAILED:email_verification_needed — the system will "
        "automatically fetch the verification code from the inbox and retry.",
        "- If you land on a non-email verification page (LinkedIn verification, "
        "phone verification, CAPTCHA challenge, identity check, or \"verify "
        "you're not a robot\"), do NOT attempt to solve it — immediately report "
        "RESULT:FAILED:login_required",
        "- If already logged in (from a previous session), proceed directly to "
        "the application form.\n",

        # ── STEP 4 ──
        "### STEP 4 — FILL THE FORM\n",
        "- Use browser_snapshot to see all visible form fields and their ref attributes.",
        "- Use the HIGH-LEVEL MCP tools to interact with the page. "
        "NEVER use browser_run_code or browser_evaluate to interact with form elements.\n",
        "- Fill fields systematically from top to bottom using these tools:\n",
        "  **browser_snapshot** — read the page and get element refs\n",
        "  **browser_click** — click buttons, links, checkboxes (pass the ref)\n",
        "  **browser_type** — type text into inputs (pass the ref)\n",
        "  **browser_fill_form** — fill multiple form fields at once\n",
        "  **browser_select_option** — select dropdown options\n",
        "  **browser_file_upload** — upload files\n",
        "  **browser_navigate** — go to a URL\n",
        "  **browser_tabs** — manage tabs\n",
        "  **Name fields**: Use the first name and last name from the profile. "
        "If a single \"Full Name\" field is shown, use the full name.",
        "  **Email**: Use the provided email address.",
        "  **Phone**: Use the provided phone number. If the form asks for country "
        "code separately, split accordingly.",
        "  **Address fields**: Use address, city, state, and zip code from the "
        "profile. If any are missing, leave them blank if optional.",
        "  **LinkedIn**: Use the provided LinkedIn URL.",
        "  **Website / Portfolio**: Use the provided website URL.",
        "  **Experience fields**: Use years_experience and resume content to "
        "fill in experience summaries or descriptions.",
        "  **Salary / compensation**: Use the compensation data from the profile. "
        "If the form asks for a specific number, use salary_expectation. If it "
        "asks for a range, use range_min and range_max.",
        "  **Work authorization questions**: Answer using the work authorization "
        "data. \"Are you authorized to work?\" -> Yes/No based on legally_authorized. "
        "\"Do you require sponsorship?\" -> Yes/No based on require_sponsorship.",
        "  **Location / relocation questions**: Answer based on the preferred "
        "locations listed in the JOB CONTEXT section. If the job requires "
        "relocation outside preferred locations and is not remote, answer honestly.",
        "  **Screening questions**: Answer using the resume text and profile data. "
        "Be honest — do not claim skills or experience not present in the resume.",
        "  **\"How did you hear about us?\"**: Answer \"Job Board\" or \"Online Search\".",
        f"  **Date fields** (start date, availability): Use \"{getattr(p, 'notice_period', '2 weeks')}\" "
        "as the notice period / earliest start date.",
        "  **Voluntary self-identification / EEO**: Use the EEO data if provided. "
        "Otherwise select \"Decline to self-identify\" or \"I do not wish to answer\".\n",

        # ── STEP 5 ──
        "### STEP 5 — UPLOAD RESUME\n",
        "- When you find a file upload field for resume or CV, use "
        "browser_file_upload with the resume file path from the RESUME section.",
        "- If the upload button triggers a file chooser dialog, the "
        "browser_file_upload tool will handle it automatically.",
        "- If there is a separate cover letter upload field and cover letter "
        "text was provided, look for a way to paste or type it into a text "
        "area. If only file upload is available for the cover letter, skip it.\n",

        # ── STEP 6 ──
        "### STEP 6 — HANDLE MULTI-PAGE FORMS\n",
        "- After filling all visible fields on the current page, look for "
        "\"Next\", \"Continue\", \"Save & Continue\", or similar buttons.",
        "- Click them and repeat STEP 4 and STEP 5 for each new page.",
        "- Keep going until you reach a review or submit page.",
        "- On review pages, verify that the key fields (name, email, resume) "
        "are correct before proceeding.\n",

        # ── STEP 7 ──
        "### STEP 7 — REVIEW & SUBMIT\n",
    ])

    if dry_run:
        lines.append(
            "**DRY RUN MODE**: DO NOT click Submit, Send Application, or any "
            "final submission button. Verify that the form is filled correctly, "
            "then output RESULT:DRY_RUN"
        )
    else:
        lines.extend([
            "- Click the Submit / Apply / Send Application button.",
            "- After clicking, wait a moment for the page to respond.",
            "- Take a snapshot to verify success — look for a confirmation "
            "message, \"Thank you for applying\", or similar.",
            "- If the site says you have already applied for this job, output "
            "RESULT:ALREADY_APPLIED",
            "",
            "**CRITICAL — AFTER SUBMISSION:**",
            "- If you see a confirmation / thank-you page → output RESULT:APPLIED",
            "- You MUST output a RESULT code as the VERY LAST thing you write.",
            "- Do NOT end your response without a RESULT code.",
        ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 9. CAPTCHA HANDLING
# ---------------------------------------------------------------------------

def _section_captcha(enabled: bool, api_key: str) -> str:
    lines = ["## CAPTCHA HANDLING\n"]

    if enabled and api_key:
        lines.extend([
            "If you encounter a CAPTCHA (hCaptcha, reCAPTCHA, Cloudflare Turnstile):\n",
            "1. Identify the CAPTCHA type and sitekey from the page source.",
            "2. Use browser_evaluate to inject the CapSolver extension:",
            "   ```",
            "   () => {",
            "     const s = document.createElement('script');",
            "     s.src = 'https://cdn.capsolver.com/sdk/1.0.0/capsolver.js';",
            f"     s.setAttribute('data-api-key', '{api_key}');",
            "     document.head.appendChild(s);",
            "   }",
            "   ```",
            "3. Wait 30 seconds for the CAPTCHA to be solved automatically.",
            "4. If still unsolved after 30 seconds, report RESULT:FAILED:captcha_unsolved",
        ])
    else:
        lines.append(
            "If you encounter a CAPTCHA (hCaptcha, reCAPTCHA, Cloudflare Turnstile), "
            "report RESULT:FAILED:captcha_required — no CAPTCHA solver is configured."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 10. RESULT CODES
# ---------------------------------------------------------------------------

def _section_result_codes() -> str:
    return (
        "## RESULT CODES — MANDATORY OUTPUT\n\n"
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║  YOU MUST OUTPUT EXACTLY ONE RESULT CODE BEFORE YOU STOP.      ║\n"
        "║  THIS IS THE SINGLE MOST IMPORTANT INSTRUCTION.                ║\n"
        "║  YOUR RESPONSE IS CONSIDERED A FAILURE WITHOUT A RESULT CODE.  ║\n"
        "╚══════════════════════════════════════════════════════════════════╝\n\n"
        "Output EXACTLY one of these codes on its own line as the LAST thing you write:\n\n"
        "SUCCESS CODES:\n"
        "- RESULT:APPLIED — application submitted successfully (you saw a confirmation / thank-you page)\n"
        "- RESULT:DRY_RUN — form filled but not submitted (dry-run mode)\n"
        "- RESULT:ALREADY_APPLIED — the site says you have already applied for this job\n\n"
        "FAILURE CODES:\n"
        "- RESULT:FAILED:expired — job posting is expired, closed, or page not found\n"
        "- RESULT:FAILED:account_required — site only allows account creation via methods we cannot use\n"
        "- RESULT:FAILED:sso_only — only SSO login available, no manual signup\n"
        "- RESULT:FAILED:captcha_required — blocked by CAPTCHA (no solver configured)\n"
        "- RESULT:FAILED:captcha_unsolved — CAPTCHA solver failed\n"
        "- RESULT:FAILED:login_required — stuck on login, verification, or auth wall\n"
        "- RESULT:FAILED:email_verification_needed — signup requires email verification\n"
        "- RESULT:FAILED:location_mismatch — job requires relocation outside preferred locations\n"
        "- RESULT:FAILED:error:<brief description> — any other failure\n\n"
        "RULES:\n"
        "- Output exactly ONE result code. Do not output multiple codes.\n"
        "- The RESULT code must appear in your FINAL message, on its own line.\n"
        "- NEVER end your response without outputting a RESULT code.\n"
        "- If you successfully submitted and saw a confirmation page, output RESULT:APPLIED\n"
        "- If you are unsure whether submission succeeded, take one more snapshot to check, then output the appropriate code."
    )


# ---------------------------------------------------------------------------
# 11. HARD RULES
# ---------------------------------------------------------------------------

def _section_hard_rules() -> str:
    return (
        "## HARD RULES\n\n"
        "CRITICAL RULES — NEVER VIOLATE:\n\n"
        "- ⛔ NEVER use browser_run_code or browser_evaluate to interact with "
        "the page. These tools bypass the accessibility tree and break element "
        "references. ALWAYS use the high-level tools: browser_snapshot, "
        "browser_click, browser_type, browser_fill_form, browser_select_option, "
        "browser_file_upload. If a high-level tool fails, try a different ref or "
        "approach — do NOT fall back to raw JavaScript.\n"
        "- ALWAYS use browser_snapshot before and after major actions to verify "
        "the page state.\n"
        "- NEVER click \"Apply with LinkedIn\", \"Easy Apply\", or any SSO / "
        "third-party sign-in button — always use the manual / direct apply path.\n"
        "- NEVER fabricate work experience, skills, metrics, or qualifications "
        "not present in the resume or profile.\n"
        "- NEVER agree to a salary below the minimum range (if range_min is specified "
        "in the compensation section).\n"
        "- If a field is optional and you do not have the data for it, leave it blank.\n"
        "- Skip fields that are already pre-filled with correct values.\n"
        "- If an action fails (click, fill, upload), retry ONCE, then move on "
        "or report failure.\n"
        "- If you are stuck on the same page for more than 3 consecutive snapshots "
        "with no progress, STOP and report RESULT:FAILED:error:stuck_on_page\n"
        "- NEVER spend more than 2 attempts on any login, verification, or auth page. "
        "If you cannot get past it, report RESULT:FAILED:login_required immediately.\n"
        "- Keep going until ALL form pages are complete — do not stop at the "
        "first page of a multi-step form.\n"
        "- Wait for pages to finish loading before interacting with elements.\n"
        "- If a dropdown does not have an exact match, choose the closest option.\n"
        "- For \"Other\" or free-text follow-up fields, provide a concise, "
        "honest answer derived from the profile data.\n\n"
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║  REMINDER: YOUR VERY LAST LINE MUST BE A RESULT CODE.          ║\n"
        "║  Example: RESULT:APPLIED  or  RESULT:FAILED:error:reason       ║\n"
        "║  NEVER end without a RESULT code — this is non-negotiable.     ║\n"
        "╚══════════════════════════════════════════════════════════════════╝"
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into (first_name, last_name)."""
    parts = full_name.strip().split()
    if not parts:
        return ("", "")
    first = parts[0]
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return (first, last)
