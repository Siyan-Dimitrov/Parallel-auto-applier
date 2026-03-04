from __future__ import annotations

import asyncio

from playwright.async_api import Page

from src.applicators.base import BaseApplicator, ApplicationResult


class LeverApplicator(BaseApplicator):
    """Fill and submit Lever ATS application forms."""

    ats_type = "lever"

    async def apply(self, page: Page, apply_url: str, cover_letter: str | None = None, dry_run: bool = False) -> ApplicationResult:
        try:
            self.log.info("[lever] Navigating to %s", apply_url)

            # Lever apply pages are typically at /apply at the end
            if "/apply" not in apply_url:
                apply_url = apply_url.rstrip("/") + "/apply"

            await page.goto(apply_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # Lever has a clean, predictable form
            # Full name
            for selector in [
                'input[name="name"]',
                'input[placeholder*="Full name"]',
                'input[name="fullName"]',
            ]:
                if await self._fill_field(page, selector, self.personal.full_name):
                    break

            # Email
            for selector in [
                'input[name="email"]',
                'input[type="email"]',
                'input[placeholder*="Email"]',
            ]:
                if await self._fill_field(page, selector, self.personal.email):
                    break

            # Phone
            for selector in [
                'input[name="phone"]',
                'input[type="tel"]',
                'input[placeholder*="Phone"]',
            ]:
                if await self._fill_field(page, selector, self.personal.phone):
                    break

            # LinkedIn
            for selector in [
                'input[name*="linkedin"]',
                'input[name="urls[LinkedIn]"]',
                'input[placeholder*="LinkedIn"]',
            ]:
                if await self._fill_field(page, selector, self.personal.linkedin_url):
                    break

            # Website
            if self.personal.website:
                for selector in [
                    'input[name*="website"]',
                    'input[name="urls[Portfolio]"]',
                    'input[name*="portfolio"]',
                    'input[placeholder*="Website"]',
                ]:
                    if await self._fill_field(page, selector, self.personal.website):
                        break

            # Current company
            if self.personal.current_company:
                for selector in [
                    'input[name*="org"]',
                    'input[name="currentCompany"]',
                    'input[placeholder*="Current company"]',
                ]:
                    if await self._fill_field(page, selector, self.personal.current_company):
                        break

            # Resume upload
            await self._upload_resume(page, 'input[type="file"]')
            await asyncio.sleep(1)

            # Cover letter (Lever uses a textarea)
            if cover_letter:
                for selector in [
                    'textarea[name="comments"]',
                    'textarea[name*="cover"]',
                    'textarea[placeholder*="Add a cover letter"]',
                    "textarea",
                ]:
                    if await self._fill_field(page, selector, cover_letter, clear=True):
                        break

            await asyncio.sleep(1)

            # Submit
            submit_selector = 'button[type="submit"], button.postings-btn, input[type="submit"]'
            if not await self._click_submit(page, submit_selector, dry_run):
                return ApplicationResult(success=False, ats_type=self.ats_type, error_message="Submit button not found")

            if not dry_run:
                await asyncio.sleep(3)

            self.log.info("[lever] Application %s", "submitted" if not dry_run else "filled (dry run)")
            return ApplicationResult(success=True, ats_type=self.ats_type)

        except Exception as e:
            self.log.error("[lever] Application failed: %s", e)
            return ApplicationResult(success=False, ats_type=self.ats_type, error_message=str(e))
