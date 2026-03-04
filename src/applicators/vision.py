from __future__ import annotations

import asyncio
import base64
import json
import re

import ollama as ollama_client
from playwright.async_api import Page

from src.applicators.base import BaseApplicator, ApplicationResult
from src.config import Config
from src.browser import BrowserManager

MAX_STEPS = 20
STEP_DELAY = 2  # seconds between actions


class VisionApplicator(BaseApplicator):
    """Vision-based applicator that uses screenshots and an agentic loop.

    Takes a screenshot of the page, sends it to a multimodal LLM which
    returns the next action (click, type, scroll, etc.), executes it,
    and repeats until the form is submitted or the agent gives up.
    """

    ats_type = "vision"

    def __init__(self, config: Config, browser: BrowserManager):
        super().__init__(config, browser)
        self.vision_model = config.ollama.vision_model
        self.client = ollama_client.Client(host=config.ollama.base_url)

    async def apply(
        self,
        page: Page,
        apply_url: str,
        cover_letter: str | None = None,
        dry_run: bool = False,
    ) -> ApplicationResult:
        try:
            self.log.info("[vision] Starting vision agent on %s", apply_url)

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

            action_history: list[str] = []

            for step in range(1, MAX_STEPS + 1):
                screenshot_b64 = await self._take_screenshot(page)

                action = self._analyze_screenshot(
                    screenshot_b64,
                    personal_info=personal_dict,
                    cover_letter=cover_letter,
                    action_history=action_history,
                    step=step,
                    dry_run=dry_run,
                )

                action_type = action.get("action", "fail")
                reason = action.get("reason", "")
                self.log.info(
                    "[vision] Step %d/%d: %s — %s",
                    step, MAX_STEPS, action_type, reason,
                )

                if action_type == "done":
                    self.log.info("[vision] Agent reports application complete.")
                    return ApplicationResult(success=True, ats_type=self.ats_type)

                if action_type == "fail":
                    return ApplicationResult(
                        success=False,
                        ats_type=self.ats_type,
                        error_message=f"Vision agent gave up: {reason}",
                    )

                success = await self._execute_action(page, action, dry_run)
                action_history.append(
                    f"Step {step}: {action_type}"
                    + (f" text='{action.get('text', '')[:30]}'" if action.get("text") else "")
                    + (f" at ({action.get('x')},{action.get('y')})" if action.get("x") is not None else "")
                    + (f" — {'OK' if success else 'FAILED'}")
                )

                await asyncio.sleep(STEP_DELAY)

            return ApplicationResult(
                success=False,
                ats_type=self.ats_type,
                error_message=f"Vision agent reached max steps ({MAX_STEPS})",
            )

        except Exception as e:
            self.log.error("[vision] Vision agent failed: %s", e)
            return ApplicationResult(
                success=False, ats_type=self.ats_type, error_message=str(e)
            )

    async def _take_screenshot(self, page: Page) -> str:
        """Take a screenshot of the visible viewport and return as base64."""
        img_bytes = await page.screenshot(full_page=False)
        return base64.b64encode(img_bytes).decode()

    def _analyze_screenshot(
        self,
        screenshot_b64: str,
        personal_info: dict,
        cover_letter: str | None,
        action_history: list[str],
        step: int,
        dry_run: bool,
    ) -> dict:
        """Send screenshot to vision model and get next action as JSON."""
        history_text = "\n".join(action_history[-10:]) if action_history else "None yet"

        system_prompt = (
            "You are a web automation agent filling out a job application form. "
            "You see a screenshot of a web page. Decide the single next action to take.\n\n"
            "Return ONLY valid JSON with these fields:\n"
            '- "action": one of "click", "type", "select", "scroll", "upload", "submit", "done", "fail"\n'
            '- "x": pixel x-coordinate to click (required for click/type/select/submit)\n'
            '- "y": pixel y-coordinate to click (required for click/type/select/submit)\n'
            '- "text": text to type (required for type action)\n'
            '- "reason": brief explanation of why you chose this action\n\n'
            "Action descriptions:\n"
            '- "click": Click at (x, y) to focus a field or toggle a checkbox/radio\n'
            '- "type": Click at (x, y) then type the text value into the field\n'
            '- "select": Click the dropdown at (x, y) to open it. On the next step you can click an option\n'
            '- "scroll": Scroll down to reveal more form fields (no x/y needed)\n'
            '- "upload": Click the file upload button/area at (x, y) to trigger resume upload\n'
            '- "submit": Click the submit/apply button at (x, y) to submit the application\n'
            '- "done": The application has been submitted successfully (e.g. you see a confirmation message)\n'
            '- "fail": You cannot complete this form (explain why in reason)\n\n'
            "Important rules:\n"
            "- Fill fields from top to bottom\n"
            "- Only fill empty fields — skip fields that already have values\n"
            "- After filling all visible fields, scroll down to check for more\n"
            "- If you see a confirmation/thank you page, return done\n"
            "- If the page looks unrelated to a job application, return fail\n"
            "- The viewport is 1280x900 pixels — coordinates must be within this range\n"
        )

        if dry_run:
            system_prompt += (
                '\n- DRY RUN MODE: When you would normally submit, return '
                '{"action": "done", "reason": "Dry run - form filled, not submitting"} '
                "instead of clicking submit.\n"
            )

        user_content = (
            f"Step {step}/{MAX_STEPS}.\n\n"
            f"## Applicant Info:\n{json.dumps(personal_info, indent=2)}\n\n"
        )
        if cover_letter:
            user_content += f"## Cover Letter:\n{cover_letter[:500]}\n\n"
        user_content += (
            f"## Resume file: {self.resume_path}\n\n"
            f"## Actions taken so far:\n{history_text}\n\n"
            "Look at the screenshot and return the NEXT action as JSON."
        )

        try:
            resp = self.client.chat(
                model=self.vision_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": user_content,
                        "images": [screenshot_b64],
                    },
                ],
            )
            response_text = resp["message"]["content"]
            return self._parse_action(response_text)
        except Exception as e:
            self.log.error("[vision] Vision model call failed: %s", e)
            return {"action": "fail", "reason": f"Vision model error: {e}"}

    def _parse_action(self, response: str) -> dict:
        """Parse the JSON action from the vision model response."""
        # Try direct JSON parse
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try finding any JSON object
        match = re.search(r"\{[^{}]*\"action\"[^{}]*\}", response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        self.log.warning("[vision] Could not parse action: %s", response[:200])
        return {"action": "fail", "reason": "Could not parse vision model response"}

    async def _execute_action(self, page: Page, action: dict, dry_run: bool) -> bool:
        """Execute a single action on the page. Returns True on success."""
        action_type = action.get("action")
        x = action.get("x")
        y = action.get("y")
        text = action.get("text", "")

        try:
            if action_type == "click":
                await page.mouse.click(x, y)
                return True

            elif action_type == "type":
                await page.mouse.click(x, y)
                await asyncio.sleep(0.3)
                # Select all existing text and replace
                await page.keyboard.press("Control+a")
                await page.keyboard.type(text, delay=30)
                return True

            elif action_type == "select":
                await page.mouse.click(x, y)
                return True

            elif action_type == "scroll":
                await page.mouse.wheel(0, 400)
                return True

            elif action_type == "upload":
                # Try to find the nearest file input and set it
                file_input = page.locator('input[type="file"]').first
                try:
                    if self.resume_path.exists():
                        await file_input.set_input_files(
                            str(self.resume_path.resolve())
                        )
                        self.log.info("[vision] Resume uploaded via file input")
                        return True
                    else:
                        self.log.warning("[vision] Resume not found at %s", self.resume_path)
                        return False
                except Exception:
                    # If no file input found, click at coordinates to trigger
                    # a file picker dialog (may not work)
                    if x is not None and y is not None:
                        await page.mouse.click(x, y)
                    return False

            elif action_type == "submit":
                if dry_run:
                    self.log.info("[vision] DRY RUN — would click submit at (%s, %s)", x, y)
                    return True
                await page.mouse.click(x, y)
                await asyncio.sleep(3)
                return True

            else:
                self.log.warning("[vision] Unknown action type: %s", action_type)
                return False

        except Exception as e:
            self.log.debug("[vision] Action %s failed: %s", action_type, e)
            return False

    def _first_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return parts[0] if parts else ""

    def _last_name(self) -> str:
        parts = self.personal.full_name.strip().split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""
