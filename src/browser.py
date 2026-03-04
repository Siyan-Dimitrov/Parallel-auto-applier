from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from src.config import BrowserConfig
from src.utils.logging import get_logger


class BrowserManager:
    """Manages a single Playwright browser instance with reusable contexts."""

    def __init__(self, config: BrowserConfig):
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.log = get_logger()

    async def start(self):
        """Launch the browser."""
        self.log.info("Launching browser (headless=%s, slow_mo=%d)", self.config.headless, self.config.slow_mo)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo,
        )

    async def stop(self):
        """Close browser and playwright."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self.log.info("Browser closed")

    @asynccontextmanager
    async def new_context(self) -> AsyncGenerator[BrowserContext, None]:
        """Create a new browser context with reasonable defaults."""
        if not self._browser:
            await self.start()
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        context.set_default_timeout(self.config.timeout)
        try:
            yield context
        finally:
            await context.close()

    @asynccontextmanager
    async def new_page(self) -> AsyncGenerator[Page, None]:
        """Convenience: open a new context with a single page."""
        async with self.new_context() as ctx:
            page = await ctx.new_page()
            yield page


async def human_type(page: Page, selector: str, text: str, delay: int = 50):
    """Type text with human-like delays."""
    await page.click(selector)
    await page.type(selector, text, delay=delay)
