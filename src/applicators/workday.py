from __future__ import annotations

import asyncio

from playwright.async_api import Page

from src.applicators.base import BaseApplicator, ApplicationResult
from src.browser import human_type


class WorkdayApplicator(BaseApplicator):
    """Fill and submit Workday ATS application forms.

    Workday is more complex — multi-step forms, iframes, and dynamic elements.
    This handles the most common Workday layout.
    """

    ats_type = "workday"

    async def apply(self, page: Page, apply_url: str, cover_letter: str | None = None, dry_run: bool = False) -> ApplicationResult:
        try:
            self.log.info("[workday] Navigating to %s", apply_url)
            await page.goto(apply_url, wait_until="networkidle")
            await asyncio.sleep(3)

            # Workday often has an "Apply" button on the job page
            await self._click_initial_apply(page)
            await asyncio.sleep(2)

            # Handle "Apply Manually" vs "Use my last application" choice
            await self._handle_apply_method(page)
            await asyncio.sleep(2)

            # Step 1: My Information
            await self._fill_my_information(page)
            await asyncio.sleep(1)

            # Try to navigate through multi-step form
            await self._click_next(page)
            await asyncio.sleep(2)

            # Step 2: My Experience (resume upload)
            await self._upload_resume(page, 'input[type="file"]')
            await asyncio.sleep(2)

            # Cover letter if there's a field for it
            if cover_letter:
                for selector in [
                    'textarea[data-automation-id*="cover"]',
                    'textarea[aria-label*="Cover"]',
                    'textarea[placeholder*="cover"]',
                ]:
                    if await self._fill_field(page, selector, cover_letter, clear=True):
                        break

            await self._click_next(page)
            await asyncio.sleep(2)

            # Final step: Submit
            submit_selectors = [
                'button[data-automation-id="bottom-navigation-next-button"]',
                'button[aria-label="Submit"]',
                'button:has-text("Submit")',
            ]

            submitted = False
            for selector in submit_selectors:
                if await self._click_submit(page, selector, dry_run):
                    submitted = True
                    break

            if not submitted:
                return ApplicationResult(
                    success=False, ats_type=self.ats_type,
                    error_message="Could not find submit button"
                )

            if not dry_run:
                await asyncio.sleep(3)

            self.log.info("[workday] Application %s", "submitted" if not dry_run else "filled (dry run)")
            return ApplicationResult(success=True, ats_type=self.ats_type)

        except Exception as e:
            self.log.error("[workday] Application failed: %s", e)
            return ApplicationResult(success=False, ats_type=self.ats_type, error_message=str(e))

    async def _click_initial_apply(self, page: Page):
        """Click the initial Apply button on the job description page."""
        for selector in [
            'a[data-automation-id="jobPostingApplyButton"]',
            'button:has-text("Apply")',
            'a:has-text("Apply")',
        ]:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    return
            except Exception:
                continue

    async def _handle_apply_method(self, page: Page):
        """If Workday asks how to apply, choose 'Apply Manually'."""
        try:
            manual = page.locator('button:has-text("Apply Manually"), a:has-text("Apply Manually")')
            if await manual.is_visible(timeout=3000):
                await manual.click()
        except Exception:
            pass  # Not all Workday forms show this

    async def _fill_my_information(self, page: Page):
        """Fill the 'My Information' step of Workday forms."""
        # These data-automation-id selectors are standard Workday
        field_map = {
            'input[data-automation-id="legalNameSection_firstName"]': self._first_name(),
            'input[data-automation-id="legalNameSection_lastName"]': self._last_name(),
            'input[data-automation-id="email"]': self.personal.email,
            'input[data-automation-id="phone-number"]': self.personal.phone,
            'input[data-automation-id="addressSection_addressLine1"]': "",
        }

        for selector, value in field_map.items():
            if value:
                await self._fill_field(page, selector, value)

    async def _click_next(self, page: Page):
        """Click the Next/Continue button in multi-step Workday forms."""
        for selector in [
            'button[data-automation-id="bottom-navigation-next-button"]',
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Save and Continue")',
        ]:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    return
            except Exception:
                continue

    def _first_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return parts[0] if parts else ""

    def _last_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""
