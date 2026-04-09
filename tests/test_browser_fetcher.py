"""Integration test for BrowserFetcher — requires Playwright + Chromium.

Skipped by default in CI.  Run with: pytest -m browser
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.mark.asyncio
async def test_fetch_html_example_com() -> None:
    """Launch Chromium, fetch https://example.com, verify HTML content."""
    try:
        from infrastructure.fetchers.browser import BrowserFetcher, BrowserPool
    except ImportError:
        pytest.skip("Playwright not installed")

    pool = BrowserPool(restart_after_pages=10, page_timeout_sec=15.0)
    fetcher = BrowserFetcher(pool)

    try:
        html = await fetcher.fetch_html("https://example.com")
        assert "<title>" in html.lower()
        assert "example" in html.lower()
    finally:
        await pool.close()
