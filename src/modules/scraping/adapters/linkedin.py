"""LinkedIn adapter — scrapes public job listings via Playwright.

Uses BrowserFetcher with anti-bot precautions: random delays between
page loads, high poll interval (4h), realistic UA, graceful auth-wall
detection.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from bs4 import BeautifulSoup

from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.browser import BrowserFetcher
from modules.scraping.signals import extract_stack

logger = structlog.get_logger()

_BASE_URL = "https://www.linkedin.com/jobs/search/"
_WAIT_SELECTOR = ".jobs-search__results-list, .job-search-card"

# Detect LinkedIn auth wall
_AUTH_WALL_INDICATORS = ("authwall", "login", "sign in to linkedin")

# Parse relative time strings: "2 hours ago", "1 day ago", "3 weeks ago"
_RELATIVE_TIME = re.compile(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", re.I)

_TIME_MULTIPLIERS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,
}


class LinkedInAdapter:
    """Scrapes public LinkedIn job listings."""

    def __init__(self, fetcher: BrowserFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "linkedin"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.linkedin_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Scrape LinkedIn job search results with anti-bot delays."""
        all_listings: list[dict[str, Any]] = []

        for query in self._settings.linkedin_search_queries:
            url = f"{_BASE_URL}?keywords={query}&f_TPR=r86400"  # last 24h filter
            log = logger.bind(adapter=self.name, query=query)

            try:
                html = await self._fetcher.fetch_html(
                    url, wait_for_selector=_WAIT_SELECTOR
                )

                # Check for auth wall
                if _is_auth_wall(html):
                    log.warning("linkedin_auth_wall_detected")
                    continue

                listings = _parse_listings(html)
                all_listings.extend(listings)
                log.debug("linkedin_fetched", listings=len(listings))

            except Exception:
                log.warning("linkedin_fetch_failed", exc_info=True)

            # Anti-bot: random delay between queries
            delay = random.uniform(
                self._settings.linkedin_min_delay_sec,
                self._settings.linkedin_max_delay_sec,
            )
            await asyncio.sleep(delay)

        return all_listings

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a parsed LinkedIn listing to a CanonicalLead.

        Pure function — no I/O.
        """
        title = raw.get("title", "")
        company = raw.get("company", "")
        if not title:
            return None

        combined = f"{title} {company} {raw.get('description', '')}"
        posted_at = _parse_relative_time(raw.get("posted_time", ""))

        return CanonicalLead(
            source="linkedin",
            source_id=raw.get("url", title),
            url=raw.get("url", ""),
            title=f"{company} — {title}" if company else title,
            body=raw.get("description", "")[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=80,  # LinkedIn jobs are high-quality signals
            company_name=company or None,
            location=raw.get("location"),
            stack_mentions=extract_stack(combined),
            posted_at=posted_at,
        )


def _is_auth_wall(html: str) -> bool:
    """Detect if LinkedIn returned an auth wall instead of results."""
    lower = html.lower()
    return any(indicator in lower for indicator in _AUTH_WALL_INDICATORS)


def _parse_listings(html: str) -> list[dict[str, Any]]:
    """Extract job card data from LinkedIn search results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    listings: list[dict[str, Any]] = []

    cards = soup.select(".job-search-card, .base-card, [data-entity-urn*='jobPosting']")
    if not cards:
        cards = soup.select("li[class*='job']")

    for card in cards:
        title_el = card.select_one(".base-search-card__title, h3, [class*='title']")
        company_el = card.select_one(".base-search-card__subtitle, h4, [class*='company']")
        location_el = card.select_one(".job-search-card__location, [class*='location']")
        time_el = card.select_one("time, [class*='date'], [class*='posted']")
        link_el = card.select_one("a[href*='/jobs/'], a[href*='linkedin.com']")

        title = title_el.get_text(strip=True) if title_el else ""
        company = company_el.get_text(strip=True) if company_el else ""
        location = location_el.get_text(strip=True) if location_el else ""
        posted_time = ""
        if time_el:
            posted_time = time_el.get("datetime", "") or time_el.get_text(strip=True)

        url = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            url = href if href.startswith("http") else f"https://www.linkedin.com{href}"

        description = card.get_text(separator=" ", strip=True)

        if title:
            listings.append({
                "title": title,
                "company": company,
                "location": location,
                "posted_time": posted_time,
                "url": url,
                "description": description,
            })

    return listings


def _parse_relative_time(text: str) -> datetime | None:
    """Convert '2 hours ago' style strings to a datetime."""
    if not text:
        return None
    match = _RELATIVE_TIME.search(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * _TIME_MULTIPLIERS.get(unit, 0)
    if not seconds:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)
