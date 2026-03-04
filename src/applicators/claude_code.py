"""Claude Code + Playwright MCP applicator.

Uses Claude Code CLI (via Ollama) with the Playwright MCP server connected
to a Chrome CDP endpoint for persistent browser sessions.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.applicators.prompt_builder import build_prompt
from src.config import Config
from src.utils.logging import get_logger

# Failure classification
PERMANENT_FAILURES = frozenset({
    "expired", "sso_only", "not_found", "location_mismatch",
})
TRANSIENT_FAILURES = frozenset({
    "timeout", "login_required", "captcha_required",
    "email_verification_needed",
})


@dataclass
class ApplicationResult:
    """Result of a Claude Code application attempt."""
    success: bool
    ats_type: str = "claude_code"
    error_message: str | None = None
    failure_type: str | None = None  # "permanent" or "transient"


class ClaudeCodeApplicator:
    """Autonomous applicator using Claude Code CLI + Playwright MCP.

    Connects Playwright to a Chrome CDP endpoint for persistent browser
    sessions (cookies, login state, extensions).
    """

    ats_type = "claude_code"

    def __init__(self, config: Config, cdp_port: int = 9222):
        self.config = config
        self.cdp_port = cdp_port
        self.model = config.ollama.vision_model
        self.timeout = 600  # 10 minutes max per application
        self.log = get_logger()

    async def apply(
        self,
        apply_url: str,
        job: dict,
        resume_text: str = "",
        tailored_resume_text: str = "",
        cover_letter: str | None = None,
        dry_run: bool = False,
        verification_code: str = "",
        verification_link: str = "",
    ) -> ApplicationResult:
        """Launch Claude Code to fill and submit the application."""
        self.log.info("[claude-code] Starting for %s", apply_url)

        if not await self._check_claude_available():
            return ApplicationResult(
                success=False,
                error_message="Ollama CLI not found. Ensure Ollama is installed and 'ollama launch claude' works.",
                failure_type="permanent",
            )

        resume_path = str(Path(self.config.application.resume_path).resolve())

        prompt = build_prompt(
            apply_url=apply_url,
            personal_info=self.config.personal_info,
            profile=self.config.profile,
            job_preferences=self.config.job_preferences,
            resume_path=resume_path,
            resume_text=resume_text,
            cover_letter=cover_letter,
            job_title=job.get("title", ""),
            job_company=job.get("company", ""),
            job_location=job.get("location", ""),
            job_description=job.get("description", ""),
            tailored_resume_text=tailored_resume_text,
            dry_run=dry_run,
            captcha_enabled=self.config.captcha.enabled,
            captcha_api_key=self.config.captcha.api_key,
            verification_code=verification_code,
            verification_link=verification_link,
        )

        mcp_config = self._build_mcp_config()
        mcp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="mcp_claude_",
            ) as f:
                json.dump(mcp_config, f)
                mcp_path = f.name

            return await self._run_claude(prompt, mcp_path)

        except asyncio.TimeoutError:
            self.log.error("[claude-code] Timed out after %ds", self.timeout)
            return ApplicationResult(
                success=False,
                error_message="timeout",
                failure_type="transient",
            )
        except Exception as e:
            self.log.error("[claude-code] Failed: %s", e)
            return ApplicationResult(
                success=False,
                error_message=str(e),
                failure_type="transient",
            )
        finally:
            if mcp_path:
                try:
                    os.unlink(mcp_path)
                except OSError:
                    pass

    # ── helpers ────────────────────────────────────────────────────────

    def _build_mcp_config(self) -> dict:
        """Build MCP config connecting Playwright to Chrome CDP endpoint."""
        return {
            "mcpServers": {
                "playwright": {
                    "command": "npx",
                    "args": [
                        "@playwright/mcp@latest",
                        f"--cdp-endpoint=http://localhost:{self.cdp_port}",
                    ],
                }
            }
        }

    async def _check_claude_available(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ollama", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0
        except (FileNotFoundError, asyncio.TimeoutError):
            return False

    async def _run_claude(self, prompt: str, mcp_config_path: str) -> ApplicationResult:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        prompt_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, prefix="prompt_claude_",
                encoding="utf-8",
            ) as pf:
                pf.write(prompt)
                prompt_path = pf.name

            claude_args = " ".join([
                "-p",
                "--max-turns", "30",
                "--strict-mcp-config",
                "--mcp-config", f'"{mcp_config_path}"',
                "--tools", '""',
                "--permission-mode", "bypassPermissions",
                "--no-session-persistence",
                "--output-format", "stream-json",
                "--verbose",
            ])

            shell_cmd = (
                f'ollama launch claude --model {self.model} '
                f'-- {claude_args} < "{prompt_path}"'
            )

            self.log.info(
                "[claude-code] Running: ollama launch claude --model %s -- -p --mcp-config ... < prompt.txt",
                self.model,
            )

            cwd = str(Path.cwd())

            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout,
            )

            output = stdout.decode(errors="replace")
            err_output = stderr.decode(errors="replace")

            if err_output:
                self.log.warning("[claude-code] stderr (last 1000): %s", err_output[-1000:])

            if proc.returncode != 0:
                self.log.warning("[claude-code] Process exited with code %d", proc.returncode)

            if output:
                self.log.info("[claude-code] stdout length: %d chars", len(output))
                debug_path = Path("data") / "claude_code_last_output.json"
                try:
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    debug_path.write_text(output, encoding="utf-8")
                    self.log.info("[claude-code] Full output saved to %s", debug_path)
                except OSError:
                    pass
            else:
                self.log.warning("[claude-code] stdout was empty")

            return self._parse_result(output)

        finally:
            if prompt_path:
                try:
                    os.unlink(prompt_path)
                except OSError:
                    pass

    # Regex patterns for RESULT codes (match anywhere in text, not just full line)
    _RE_APPLIED = re.compile(r"RESULT:(APPLIED|SUCCESS)\b")
    _RE_DRY_RUN = re.compile(r"RESULT:DRY_RUN\b")
    _RE_ALREADY = re.compile(r"RESULT:ALREADY_APPLIED\b")
    _RE_FAILED = re.compile(r"RESULT:FAILED:(\S+)")

    # Success indicators in page URLs / titles from tool results
    _SUCCESS_URL_PATTERNS = (
        "/confirmation", "/thank-you", "/thankyou", "/success",
        "/application-submitted", "/application-received",
    )
    _SUCCESS_PHRASES = (
        "application submitted",
        "thank you for applying",
        "application received",
        "successfully submitted",
        "thank you for your application",
        "thanks for applying",
        "your application has been",
    )

    def _parse_result(self, output: str) -> ApplicationResult:
        """Extract RESULT: code from Claude Code stream-json output.

        Parses all message types including tool_result content (snapshots,
        navigation results) to detect success even when the model forgets
        to output a RESULT code.
        """
        full_text = ""
        tool_result_text = ""
        tool_uses = []

        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                msg_type = data.get("type", "")

                if msg_type == "result":
                    result_text = data.get("result", "")
                    if isinstance(result_text, str):
                        full_text += "\n" + result_text

                elif msg_type == "assistant":
                    content = data.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    full_text += "\n" + block.get("text", "")
                                elif block.get("type") == "tool_use":
                                    tool_uses.append(block.get("name", "unknown"))
                    elif isinstance(content, str):
                        full_text += "\n" + content

                elif msg_type == "user":
                    # Extract text from tool_result blocks (snapshots, navigation)
                    content = data.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                tr_content = block.get("content", "")
                                if isinstance(tr_content, str):
                                    tool_result_text += "\n" + tr_content
                                elif isinstance(tr_content, list):
                                    for item in tr_content:
                                        if isinstance(item, dict) and item.get("type") == "text":
                                            tool_result_text += "\n" + item.get("text", "")
                    # Also check the shortcut "tool_use_result" field
                    tr_shortcut = data.get("tool_use_result", "")
                    if isinstance(tr_shortcut, str) and tr_shortcut:
                        tool_result_text += "\n" + tr_shortcut

            except json.JSONDecodeError:
                full_text += "\n" + line

        if not full_text.strip():
            full_text = output

        if tool_uses:
            self.log.info("[claude-code] Tools used: %s", ", ".join(tool_uses))
        self.log.info("[claude-code] Model text length: %d, tool result text length: %d",
                      len(full_text), len(tool_result_text))

        # ── 1. Search for explicit RESULT codes in model text ──
        result = self._find_result_code(full_text)
        if result:
            return result

        # ── 2. Check tool_result text for RESULT codes (model sometimes
        #       outputs them in a context where they land in tool results) ──
        result = self._find_result_code(tool_result_text)
        if result:
            return result

        # ── 3. Fallback: detect success from model text ──
        combined_lower = full_text.lower()
        if any(phrase in combined_lower for phrase in self._SUCCESS_PHRASES):
            self.log.info("[claude-code] Detected implicit success from model text")
            return ApplicationResult(success=True)

        # ── 4. Fallback: detect success from tool results (page URLs, titles,
        #       snapshot content like "Thank you for applying") ──
        tr_lower = tool_result_text.lower()
        if any(phrase in tr_lower for phrase in self._SUCCESS_PHRASES):
            self.log.info("[claude-code] Detected implicit success from tool result content")
            return ApplicationResult(success=True)

        # Check for success URL patterns in tool result text (page URLs from
        # navigation results or snapshot tab listings)
        if any(pattern in tr_lower for pattern in self._SUCCESS_URL_PATTERNS):
            self.log.info("[claude-code] Detected implicit success from page URL pattern")
            return ApplicationResult(success=True)

        self.log.warning("[claude-code] No RESULT code found in output")
        self.log.warning("[claude-code] Model text (last 2000):\n%s", full_text[-2000:])
        return ApplicationResult(
            success=False,
            error_message="No RESULT code in Claude Code output",
            failure_type="transient",
        )

    def _find_result_code(self, text: str) -> ApplicationResult | None:
        """Search text for RESULT: codes using regex (handles inline occurrences)."""
        # Check for success codes
        if self._RE_APPLIED.search(text):
            self.log.info("[claude-code] Application submitted successfully")
            return ApplicationResult(success=True)

        if self._RE_DRY_RUN.search(text):
            self.log.info("[claude-code] Dry run completed")
            return ApplicationResult(success=True)

        if self._RE_ALREADY.search(text):
            self.log.info("[claude-code] Already applied")
            return ApplicationResult(success=True, error_message="already_applied")

        # Check for failure codes
        match = self._RE_FAILED.search(text)
        if match:
            reason = match.group(1).strip("`.,;:!\"'")  # strip markdown/punctuation
            failure_type = self._classify_failure(reason)
            self.log.warning("[claude-code] Application failed: %s (%s)", reason, failure_type)
            return ApplicationResult(
                success=False,
                error_message=reason,
                failure_type=failure_type,
            )

        return None

    @staticmethod
    def _classify_failure(reason: str) -> str:
        """Classify a failure reason as permanent or transient."""
        reason_lower = reason.lower().strip()
        if reason_lower in PERMANENT_FAILURES:
            return "permanent"
        if reason_lower in TRANSIENT_FAILURES:
            return "transient"
        if reason_lower.startswith("error:"):
            return "transient"
        return "transient"
