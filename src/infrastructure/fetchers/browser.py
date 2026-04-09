"""BrowserPool & BrowserFetcher — Playwright-based JS-rendered page fetching.

Playwright is an OPTIONAL dependency.  It is imported lazily inside
BrowserPool.__init__ and raises a clear ImportError if missing.
The DI container only constructs this when ENABLE_BROWSER_FETCHER=true.

Architecture: one Chromium browser, many pages.  Never one browser per
request — that leaks memory and Chromium processes.  After a configurable
number of pages (default 100), the browser is recycled to dodge the slow
memory leak inherent in long-running Chromium instances.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog

from infrastructure.fetchers.base import BrowserTimeoutError

logger = structlog.get_logger()

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Resource types to block — cuts bandwidth ~80% and doubles speed
_BLOCKED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "woff", "woff2", "css"}


class BrowserPool:
    """Manages a single shared Chromium instance with page recycling.

    Pages are created via the async context manager ``page()`` which
    ensures cleanup even on exceptions.  After ``restart_after_pages``
    pages, the browser is closed and relaunched on next call.
    """

    def __init__(
        self,
        *,
        restart_after_pages: int = 100,
        page_timeout_sec: float = 30.0,
        user_agent: str = DEFAULT_UA,
        viewport: tuple[int, int] = (1280, 800),
    ) -> None:
        # Lazy import — Playwright is optional
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Playwright is required for BrowserFetcher. Install it with:\n"
                '  pip install "lead-pipeline[browser]"\n'
                "  playwright install chromium"
            ) from exc

        self._restart_after = restart_after_pages
        self._page_timeout_ms = int(page_timeout_sec * 1000)
        self._user_agent = user_agent
        self._viewport = {"width": viewport[0], "height": viewport[1]}

        self._playwright: object | None = None
        self._browser: object | None = None
        self._page_count = 0
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> object:
        """Launch or recycle the browser. Must be called under self._lock."""
        from playwright.async_api import async_playwright

        needs_restart = (
            self._browser is None
            or self._page_count >= self._restart_after
        )

        if needs_restart:
            await self._close_browser()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(  # type: ignore[union-attr]
                headless=True,
            )
            self._page_count = 0
            logger.info("browser_launched")

        return self._browser  # type: ignore[return-value]

    @asynccontextmanager
    async def page(self) -> AsyncIterator[object]:
        """Provide a browser page with resource blocking and timeout.

        Usage::

            async with pool.page() as page:
                await page.goto(url)
                html = await page.content()
        """
        async with self._lock:
            browser = await self._ensure_browser()
            self._page_count += 1

        context = await browser.new_context(  # type: ignore[union-attr]
            user_agent=self._user_agent,
            viewport=self._viewport,
        )

        # Block heavy resources
        async def _block_resources(route: object) -> None:
            url = route.request.url  # type: ignore[union-attr]
            ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
            if ext in _BLOCKED_EXTENSIONS:
                await route.abort()  # type: ignore[union-attr]
            else:
                await route.continue_()  # type: ignore[union-attr]

        await context.route("**/*", _block_resources)  # type: ignore[union-attr]
        page = await context.new_page()  # type: ignore[union-attr]
        page.set_default_timeout(self._page_timeout_ms)  # type: ignore[union-attr]

        try:
            yield page
        finally:
            await context.close()  # type: ignore[union-attr]

    async def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()  # type: ignore[union-attr]
            except Exception:
                logger.warning("browser_close_error", exc_info=True)
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()  # type: ignore[union-attr]
            except Exception:
                pass
            self._playwright = None

    async def close(self) -> None:
        """Shut down the browser cleanly."""
        async with self._lock:
            await self._close_browser()
            logger.info("browser_pool_closed")


class BrowserFetcher:
    """Simple interface for adapters that just need rendered HTML.

    For richer interactions, access ``pool`` directly.
    """

    def __init__(self, pool: BrowserPool) -> None:
        self.pool = pool

    async def fetch_html(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        timeout_sec: float | None = None,
    ) -> str:
        """Navigate to URL and return the rendered HTML."""
        log = logger.bind(url=url)

        try:
            async with self.pool.page() as page:
                if timeout_sec:
                    page.set_default_timeout(int(timeout_sec * 1000))  # type: ignore[union-attr]

                await page.goto(url, wait_until="domcontentloaded")  # type: ignore[union-attr]

                if wait_for_selector:
                    await page.wait_for_selector(wait_for_selector)  # type: ignore[union-attr]

                html: str = await page.content()  # type: ignore[union-attr]
                log.debug("browser_fetched", length=len(html))
                return html

        except Exception as exc:
            if "timeout" in str(exc).lower():
                raise BrowserTimeoutError(
                    f"Page timed out: {url}", url=url
                ) from exc
            raise
