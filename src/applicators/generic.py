from __future__ import annotations

import asyncio
import json

from playwright.async_api import Page

from src.ai_matcher import AIMatcher
from src.applicators.base import BaseApplicator, ApplicationResult
from src.browser import human_type


class GenericApplicator(BaseApplicator):
    """AI-powered generic form filler for unknown ATS platforms.

    Uses Ollama to analyze form HTML and determine how to fill fields.
    """

    ats_type = "generic"

    def __init__(self, config, browser, ai_matcher: AIMatcher):
        super().__init__(config, browser)
        self.ai = ai_matcher

    async def apply(self, page: Page, apply_url: str, cover_letter: str | None = None, dry_run: bool = False) -> ApplicationResult:
        """Try HTML-based form filling first, fall back to vision agent."""
        # Navigate once — both approaches share the same page
        self.log.info("[generic] Navigating to %s", apply_url)
        await page.goto(apply_url, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Try 1: HTML-based approach
        result = await self._try_html_approach(page, cover_letter, dry_run)
        if result.success:
            return result

        # Try 2: Vision agent fallback
        self.log.info(
            "[generic] HTML approach failed (%s), trying vision agent...",
            result.error_message,
        )
        try:
            from src.applicators.vision import VisionApplicator
            vision = VisionApplicator(self.config, self.browser)
            return await vision.apply(page, apply_url, cover_letter, dry_run)
        except Exception as e:
            self.log.error("[generic] Vision fallback also failed: %s", e)
            return ApplicationResult(
                success=False, ats_type=self.ats_type,
                error_message=f"Both HTML and vision approaches failed. HTML: {result.error_message}; Vision: {e}",
            )

    async def _try_html_approach(self, page: Page, cover_letter: str | None = None, dry_run: bool = False) -> ApplicationResult:
        """Original HTML-based form detection and filling."""
        try:
            # Extract form HTML for AI analysis
            form_html = await self._extract_form_html(page)
            if not form_html:
                return ApplicationResult(
                    success=False, ats_type=self.ats_type,
                    error_message="No form found on page"
                )

            # Build personal info dict for AI
            personal_dict = {
                "full_name": self.personal.full_name,
                "first_name": self._first_name(),
                "last_name": self._last_name(),
                "email": self.personal.email,
                "phone": self.personal.phone,
                "linkedin_url": self.personal.linkedin_url,
                "website": self.personal.website,
                "current_company": self.personal.current_company,
                "years_experience": self.personal.years_experience,
            }

            if cover_letter:
                personal_dict["cover_letter"] = cover_letter

            # Ask AI to map form fields
            self.log.info("[generic] Asking AI to analyze form fields...")
            field_mapping = self.ai.identify_form_fields(form_html, personal_dict)

            if not field_mapping:
                return ApplicationResult(
                    success=False, ats_type=self.ats_type,
                    error_message="AI could not identify form fields"
                )

            self.log.info("[generic] AI identified %d fields to fill", len(field_mapping))

            # Fill each field
            filled = 0
            for selector, value in field_mapping.items():
                if not value:
                    continue
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=2000):
                        tag = await el.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "select":
                            await el.select_option(label=str(value), timeout=3000)
                        elif tag == "textarea":
                            await el.clear()
                            await human_type(page, selector, str(value), delay=30)
                        else:
                            input_type = await el.get_attribute("type") or "text"
                            if input_type == "file":
                                # Skip file inputs — handle separately
                                continue
                            elif input_type in ("checkbox", "radio"):
                                if str(value).lower() in ("true", "yes", "1"):
                                    await el.check()
                            else:
                                await el.clear()
                                await human_type(page, selector, str(value), delay=30)
                        filled += 1
                except Exception as e:
                    self.log.debug("[generic] Could not fill %s: %s", selector, e)

            self.log.info("[generic] Filled %d of %d fields", filled, len(field_mapping))

            if filled == 0:
                return ApplicationResult(
                    success=False, ats_type=self.ats_type,
                    error_message="Could not fill any form fields (selectors did not match)"
                )

            # Upload resume
            await self._upload_resume(page, 'input[type="file"]')
            await asyncio.sleep(1)

            # Find and click submit
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Submit")',
                'button:has-text("Apply")',
                'button:has-text("Send")',
                'input[value="Submit"]',
                'input[value="Apply"]',
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

            self.log.info("[generic] Application %s", "submitted" if not dry_run else "filled (dry run)")
            return ApplicationResult(success=True, ats_type=self.ats_type)

        except Exception as e:
            self.log.error("[generic] HTML approach failed: %s", e)
            return ApplicationResult(success=False, ats_type=self.ats_type, error_message=str(e))

    async def _extract_form_html(self, page: Page) -> str | None:
        """Extract the main application form HTML from the page.

        First tries to find a <form> element with inputs. If none found
        (common on SPA sites), finds the nearest common ancestor of all
        input/textarea/select elements on the page.
        """
        try:
            form_html = await page.evaluate("""
                () => {
                    const MAX_HTML = 8000;

                    // --- Try 1: find <form> with the most inputs ---
                    const forms = document.querySelectorAll('form');
                    if (forms.length > 0) {
                        let bestForm = forms[0];
                        let maxInputs = 0;
                        forms.forEach(f => {
                            const inputs = f.querySelectorAll('input, textarea, select');
                            if (inputs.length > maxInputs) {
                                maxInputs = inputs.length;
                                bestForm = f;
                            }
                        });
                        if (maxInputs > 0) {
                            const html = bestForm.outerHTML;
                            return html.length > MAX_HTML ? html.slice(0, MAX_HTML) : html;
                        }
                    }

                    // --- Try 2: formless page — find inputs anywhere ---
                    const inputs = document.querySelectorAll(
                        'input:not([type="hidden"]), textarea, select'
                    );
                    if (inputs.length === 0) return null;

                    // Find the nearest common ancestor of all inputs
                    function getAncestors(el) {
                        const path = [];
                        while (el) { path.push(el); el = el.parentElement; }
                        return path;
                    }

                    let ancestor = inputs[0];
                    for (let i = 1; i < inputs.length; i++) {
                        const pathA = getAncestors(ancestor);
                        const pathB = getAncestors(inputs[i]);
                        const setB = new Set(pathB);
                        ancestor = pathA.find(el => setB.has(el)) || document.body;
                    }

                    // Walk up at most 2 levels if the ancestor IS an input
                    if (ancestor.matches && ancestor.matches('input, textarea, select')) {
                        ancestor = ancestor.parentElement?.parentElement || ancestor.parentElement || ancestor;
                    }

                    // Don't return the whole body — if ancestor is body, pick
                    // the child of body that contains the most inputs
                    if (ancestor === document.body || ancestor === document.documentElement) {
                        let bestChild = null;
                        let bestCount = 0;
                        for (const child of document.body.children) {
                            const count = child.querySelectorAll('input, textarea, select').length;
                            if (count > bestCount) {
                                bestCount = count;
                                bestChild = child;
                            }
                        }
                        if (bestChild) ancestor = bestChild;
                    }

                    const html = ancestor.outerHTML;
                    return html.length > MAX_HTML ? html.slice(0, MAX_HTML) : html;
                }
            """)
            return form_html
        except Exception as e:
            self.log.debug("[generic] Could not extract form: %s", e)
            return None

    def _first_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return parts[0] if parts else ""

    def _last_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""
