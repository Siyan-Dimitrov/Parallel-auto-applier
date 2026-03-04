from __future__ import annotations

import asyncio

from playwright.async_api import Page

from src.applicators.base import BaseApplicator, ApplicationResult


class GreenhouseApplicator(BaseApplicator):
    """Fill and submit Greenhouse ATS application forms."""

    ats_type = "greenhouse"

    async def apply(self, page: Page, apply_url: str, cover_letter: str | None = None, dry_run: bool = False) -> ApplicationResult:
        try:
            self.log.info("[greenhouse] Navigating to %s", apply_url)
            await page.goto(apply_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # Greenhouse forms typically have these fields
            # First name
            await self._fill_field(page, "#first_name", self._first_name())
            # Last name
            await self._fill_field(page, "#last_name", self._last_name())
            # Email
            await self._fill_field(page, "#email", self.personal.email)
            # Phone
            await self._fill_field(page, "#phone", self.personal.phone)

            # LinkedIn (Greenhouse often has this)
            for selector in [
                'input[name*="linkedin"]',
                'input[placeholder*="LinkedIn"]',
                'input[id*="linkedin"]',
                'input[autocomplete="url"]',
            ]:
                if await self._fill_field(page, selector, self.personal.linkedin_url):
                    break

            # Website/portfolio
            for selector in [
                'input[name*="website"]',
                'input[name*="portfolio"]',
                'input[placeholder*="Website"]',
            ]:
                if self.personal.website and await self._fill_field(page, selector, self.personal.website):
                    break

            # Resume upload
            await self._upload_resume(page, 'input[type="file"]')
            await asyncio.sleep(1)

            # Cover letter
            if cover_letter:
                for selector in [
                    "#cover_letter",
                    'textarea[name*="cover"]',
                    'textarea[id*="cover"]',
                    'textarea[placeholder*="cover letter"]',
                ]:
                    if await self._fill_field(page, selector, cover_letter, clear=True):
                        break

            await asyncio.sleep(1)

            # Submit
            submit_selector = 'input[type="submit"], button[type="submit"], #submit_app'
            if not await self._click_submit(page, submit_selector, dry_run):
                return ApplicationResult(success=False, ats_type=self.ats_type, error_message="Submit button not found")

            if not dry_run:
                await asyncio.sleep(3)

            self.log.info("[greenhouse] Application %s", "submitted" if not dry_run else "filled (dry run)")
            return ApplicationResult(success=True, ats_type=self.ats_type)

        except Exception as e:
            self.log.error("[greenhouse] Application failed: %s", e)
            return ApplicationResult(success=False, ats_type=self.ats_type, error_message=str(e))

    def _first_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return parts[0] if parts else ""

    def _last_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""
