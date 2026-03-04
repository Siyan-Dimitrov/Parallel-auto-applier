from __future__ import annotations

from playwright.async_api import Page

from src.utils.logging import get_logger

# URL-based patterns
ATS_URL_PATTERNS = {
    "greenhouse": ["greenhouse.io", "boards.greenhouse"],
    "lever": ["lever.co", "jobs.lever"],
    "workday": ["workday.com", "myworkdayjobs"],
    "bamboohr": ["bamboohr.com"],
    "smartrecruiters": ["smartrecruiters.com"],
    "icims": ["icims.com"],
    "jobvite": ["jobvite.com"],
}

# Page content patterns (checked if URL doesn't match)
ATS_CONTENT_PATTERNS = {
    "greenhouse": ["greenhouse", "application.submit", "gh_jid"],
    "lever": ["lever.co", "lever-application"],
    "workday": ["workday", "wd5.myworkdayjobs", "workdaycdn"],
    "bamboohr": ["bamboohr"],
}


def detect_ats_from_url(url: str) -> str:
    """Detect ATS type from the URL alone."""
    url_lower = url.lower()
    for ats_type, patterns in ATS_URL_PATTERNS.items():
        if any(p in url_lower for p in patterns):
            return ats_type
    return "generic"


async def detect_ats(url: str, page: Page | None = None) -> str:
    """Detect ATS type from URL and optionally page content."""
    log = get_logger()

    # First try URL-based detection
    ats = detect_ats_from_url(url)
    if ats != "generic":
        log.info("ATS detected from URL: %s", ats)
        return ats

    # If we have a page, check content
    if page:
        try:
            content = await page.content()
            content_lower = content.lower()
            for ats_type, patterns in ATS_CONTENT_PATTERNS.items():
                if any(p in content_lower for p in patterns):
                    log.info("ATS detected from page content: %s", ats_type)
                    return ats_type
        except Exception as e:
            log.debug("Could not read page content for ATS detection: %s", e)

    log.info("No known ATS detected, using generic applicator")
    return "generic"
