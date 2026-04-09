"""Wellfound (formerly AngelList) adapter — scrapes startup job listings.

Requires BrowserFetcher (Playwright) because Wellfound is a JS-rendered
React app with no public API.  Parses rendered HTML with BeautifulSoup.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from typing import Any

import structlog
from bs4 import BeautifulSoup

from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.browser import BrowserFetcher
from modules.scraping.signals import extract_stack

logger = structlog.get_logger()

_BASE_URL = "https://wellfound.com/role"

# CSS selectors — will need updating if Wellfound changes their markup
_JOB_CARD_SELECTOR = "[data-test='StartupResult'], .styles_component__kMdTH, .job-listing"
_WAIT_SELECTOR = ".styles_results__ZQhDf, [data-test='StartupResult'], .job-listing"


class WellfoundAdapter:
    """Scrapes startup job listings from Wellfound."""

    def __init__(self, fetcher: BrowserFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "wellfound"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.wellfound_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Render Wellfound job pages and extract listing data."""
        all_listings: list[dict[str, Any]] = []

        for role in self._settings.wellfound_search_roles:
            url = f"{_BASE_URL}/{role}"
            log = logger.bind(adapter=self.name, role=role)

            try:
                html = await self._fetcher.fetch_html(
                    url, wait_for_selector=_WAIT_SELECTOR
                )
                listings = _parse_listings(html)
                all_listings.extend(listings)
                log.debug("wellfound_fetched", listings=len(listings))
            except Exception:
                log.warning("wellfound_fetch_failed", exc_info=True)

        return all_listings

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a parsed Wellfound listing to a CanonicalLead.

        Pure function — no I/O.
        """
        title = raw.get("title", "")
        company = raw.get("company", "")
        if not title:
            return None

        body = raw.get("tags", "")
        combined = f"{title} {company} {body}"

        return CanonicalLead(
            source="wellfound",
            source_id=raw.get("url", raw.get("title", "")),
            url=raw.get("url", ""),
            title=f"{company} — {title}" if company else title,
            body=body[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=75,  # startup job board = strong signal
            company_name=company or None,
            company_domain=raw.get("domain"),
            location=raw.get("location"),
            stack_mentions=extract_stack(combined),
        )


def _parse_listings(html: str) -> list[dict[str, Any]]:
    """Extract job listing data from rendered Wellfound HTML."""
    soup = BeautifulSoup(html, "html.parser")
    listings: list[dict[str, Any]] = []

    # Try multiple selector strategies for resilience
    cards = soup.select(_JOB_CARD_SELECTOR)
    if not cards:
        # Fallback: look for any link-heavy containers
        cards = soup.select("div[class*='job'], div[class*='listing'], div[class*='startup']")

    for card in cards:
        # Extract what we can from the card structure
        title_el = card.select_one("h2, h3, [class*='title'], [class*='role']")
        company_el = card.select_one("h1, [class*='company'], [class*='startup']")
        location_el = card.select_one("[class*='location']")
        link_el = card.select_one("a[href*='/jobs/'], a[href*='/role/']")

        title = title_el.get_text(strip=True) if title_el else ""
        company = company_el.get_text(strip=True) if company_el else ""
        location = location_el.get_text(strip=True) if location_el else ""
        url = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            url = href if href.startswith("http") else f"https://wellfound.com{href}"

        # Collect all visible text as tags for stack detection
        tags = card.get_text(separator=" ", strip=True)

        if title or company:
            listings.append({
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "tags": tags,
            })

    return listings
