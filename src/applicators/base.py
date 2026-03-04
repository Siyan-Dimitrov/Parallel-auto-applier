from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page

from src.browser import BrowserManager, human_type
from src.config import Config
from src.utils.logging import get_logger


@dataclass
class ApplicationResult:
    success: bool
    ats_type: str
    error_message: str | None = None


class BaseApplicator(ABC):
    """Abstract base for ATS-specific form fillers."""

    ats_type: str = "unknown"

    def __init__(self, config: Config, browser: BrowserManager):
        self.config = config
        self.browser = browser
        self.personal = config.personal_info
        self.log = get_logger()
        self.resume_path = Path(config.application.resume_path)

    @abstractmethod
    async def apply(self, page: Page, apply_url: str, cover_letter: str | None = None, dry_run: bool = False) -> ApplicationResult:
        """Fill and submit the application form.

        Args:
            page: Playwright page navigated to the apply URL
            apply_url: Direct application URL
            cover_letter: Optional cover letter text
            dry_run: If True, fill form but don't submit

        Returns:
            ApplicationResult with success status
        """
        ...

    async def _fill_field(self, page: Page, selector: str, value: str, clear: bool = True):
        """Fill a form field safely."""
        try:
            el = page.locator(selector)
            if await el.is_visible(timeout=3000):
                if clear:
                    await el.clear()
                await human_type(page, selector, value)
                return True
        except Exception as e:
            self.log.debug("Could not fill %s: %s", selector, e)
        return False

    async def _select_option(self, page: Page, selector: str, value: str):
        """Select a dropdown option."""
        try:
            el = page.locator(selector)
            if await el.is_visible(timeout=3000):
                await el.select_option(value=value, timeout=5000)
                return True
        except Exception:
            # Try by label
            try:
                await page.locator(selector).select_option(label=value, timeout=5000)
                return True
            except Exception as e:
                self.log.debug("Could not select %s: %s", selector, e)
        return False

    async def _upload_resume(self, page: Page, selector: str = 'input[type="file"]'):
        """Upload resume PDF."""
        if not self.resume_path.exists():
            self.log.warning("Resume not found at %s, skipping upload", self.resume_path)
            return False
        try:
            file_input = page.locator(selector).first
            await file_input.set_input_files(str(self.resume_path.resolve()))
            self.log.info("Resume uploaded")
            return True
        except Exception as e:
            self.log.warning("Resume upload failed: %s", e)
            return False

    async def _click_submit(self, page: Page, selector: str, dry_run: bool = False) -> bool:
        """Click the submit button (or just log in dry-run mode)."""
        if dry_run:
            self.log.info("[DRY RUN] Would click submit: %s", selector)
            return True
        try:
            await page.locator(selector).click()
            self.log.info("Clicked submit button")
            return True
        except Exception as e:
            self.log.error("Failed to click submit: %s", e)
            return False
