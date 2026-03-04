"""Read-only Gmail IMAP reader for extracting verification codes and links.

STRICTLY READ-ONLY: This module NEVER deletes, moves, flags, or modifies
any emails. Connections are opened with readonly=True at the IMAP protocol level.
"""
from __future__ import annotations

import re
import time
import datetime

from imap_tools import MailBox, AND

from src.utils.logging import get_logger

# Subjects that indicate a verification email
_VERIFICATION_SUBJECTS = re.compile(
    r"(verif|confirm|activate|code|one.time|otp|security.code|sign.?up|registration)",
    re.IGNORECASE,
)

# 4-8 digit verification codes (standalone, not part of longer numbers)
_CODE_PATTERN = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

# Verification URLs — links containing verify/confirm/activate/token
_LINK_PATTERN = re.compile(
    r"https?://[^\s\"'<>]+(?:verif|confirm|activate|token|validate|auth)[^\s\"'<>]*",
    re.IGNORECASE,
)


class EmailReader:
    """Read-only IMAP email reader for fetching verification codes/links."""

    def __init__(self, imap_host: str, email: str, app_password: str):
        self.imap_host = imap_host
        self.email = email
        self.app_password = app_password
        self.log = get_logger()

    def wait_for_verification(
        self,
        timeout: int = 120,
        poll_interval: int = 10,
        since_minutes: int = 5,
    ) -> dict | None:
        """Poll inbox (read-only) for a verification email.

        Returns:
            {"type": "code", "value": "123456"} or
            {"type": "link", "value": "https://..."} or
            None if timeout.
        """
        self.log.info("[email] Waiting up to %ds for verification email...", timeout)
        deadline = time.time() + timeout

        while time.time() < deadline:
            result = self._check_for_verification(since_minutes)
            if result:
                self.log.info(
                    "[email] Found verification %s: %s",
                    result["type"], result["value"][:80],
                )
                return result

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait = min(poll_interval, remaining)
            self.log.debug("[email] No verification email yet, retrying in %.0fs...", wait)
            time.sleep(wait)

        self.log.warning("[email] Timed out waiting for verification email after %ds", timeout)
        return None

    def _check_for_verification(self, since_minutes: int = 5) -> dict | None:
        """Search inbox for recent verification emails. Read-only."""
        since_date = datetime.date.today()
        cutoff = datetime.datetime.now() - datetime.timedelta(minutes=since_minutes)

        try:
            with MailBox(self.imap_host).login(
                self.email, self.app_password, initial_folder=None
            ) as mailbox:
                mailbox.folder.set("INBOX", readonly=True)
                # Fetch recent emails, newest first
                messages = list(mailbox.fetch(
                    AND(date_gte=since_date),
                    reverse=True,
                    limit=20,
                ))
        except Exception as e:
            self.log.error("[email] IMAP connection failed: %s", e)
            return None

        for msg in messages:
            # Skip emails older than cutoff
            if msg.date and msg.date.replace(tzinfo=None) < cutoff:
                continue

            # Check if subject looks like verification
            if not _VERIFICATION_SUBJECTS.search(msg.subject or ""):
                continue

            # Try to extract a code
            code = self._extract_code(msg.text or "")
            if not code and msg.html:
                code = self._extract_code(msg.html)
            if code:
                return {"type": "code", "value": code}

            # Try to extract a verification link
            link = self._extract_verification_link(msg.text or "", msg.html or "")
            if link:
                return {"type": "link", "value": link}

        return None

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """Extract a verification code (4-8 digits) from text."""
        # Look for codes near verification-related keywords
        for line in text.splitlines():
            if re.search(r"(code|verif|otp|pin|token)", line, re.IGNORECASE):
                match = _CODE_PATTERN.search(line)
                if match:
                    return match.group(1)

        # Fallback: any standalone 6-digit number (most common OTP length)
        six_digit = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if six_digit:
            return six_digit.group(1)

        return None

    @staticmethod
    def _extract_verification_link(text: str, html: str) -> str | None:
        """Extract a verification URL from email text or HTML."""
        # Check plain text first
        match = _LINK_PATTERN.search(text)
        if match:
            return match.group(0).rstrip(".,;)")

        # Check HTML
        match = _LINK_PATTERN.search(html)
        if match:
            return match.group(0).rstrip(".,;)\"'")

        return None
