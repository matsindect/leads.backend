"""HackerNews "Who is Hiring?" adapter — pulls every top-level comment
from the monthly thread posted by the ``whoishiring`` user.

Posted on the 1st of every month, each top-level comment is a company
actively hiring — usually with role, location, stack, and contact info.
Highest-signal hiring source available (hand-curated, active, technical).

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import SignalClassifier

logger = structlog.get_logger()

_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"

# Strip HTML tags from Algolia comment_text (comments are HTML-rendered)
_HTML_TAG = re.compile(r"<[^>]+>")

# Typical first-line patterns: "Company | Role | Location" or "Company: Role"
_FIRST_LINE_COMPANY = re.compile(r"^([^|:\n]+?)\s*[|:]\s*", re.IGNORECASE)

# Loose email match — the domain is our company_domain candidate
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")

# URL match for landing pages/job boards
_URL = re.compile(
    r"https?://([\w.-]+\.[a-z]{2,})(?:/[^\s<>\"]*)?", re.IGNORECASE
)

# Common location markers: "| SF, CA |" or "Remote" or "Location: X"
_LOCATION_INLINE = re.compile(
    r"(?:\||\b)\s*"
    r"(Remote(?:\s*\([^)]+\))?|"
    r"Hybrid(?:\s*\([^)]+\))?|"
    r"Onsite(?:\s*\([^)]+\))?|"
    r"[A-Z][A-Za-z]+(?:,\s*[A-Z]{2,3}|\s+[A-Z][A-Za-z]+)?)\s*(?:\||$)",
)


class HNHiringAdapter:
    """Fetches comments from the current month's HN 'Who is hiring?' thread."""

    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "hnhiring"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.hnhiring_poll_interval_seconds

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            notes="No parameters — always fetches the current month's HN 'Who is hiring?' thread.",
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        """Find the latest 'Who is hiring?' thread, then fetch its comments."""
        thread_id = await self._find_latest_thread_id()
        if thread_id is None:
            logger.warning("hnhiring_thread_not_found")
            return []

        log = logger.bind(adapter=self.name, thread_id=thread_id)
        comments = await self._fetch_top_level_comments(thread_id)
        log.info("hnhiring_fetched", count=len(comments))
        return comments

    async def _find_latest_thread_id(self) -> str | None:
        """Locate the current month's thread ID.

        The ``whoishiring`` user posts one 'Ask HN: Who is hiring?' story
        at the start of each month. We sort by created_at desc and take
        the first match.
        """
        resp = await self._fetcher.get_json(
            _ALGOLIA_URL,
            params={
                "query": "Ask HN: Who is hiring?",
                "tags": "(story,author_whoishiring)",
                "hitsPerPage": "1",
            },
        )
        hits = resp.data.get("hits") or []
        if not hits:
            return None
        return str(hits[0].get("objectID", "")) or None

    async def _fetch_top_level_comments(
        self, thread_id: str
    ) -> list[dict[str, Any]]:
        """Pull every top-level comment on the thread.

        Uses Algolia's comment tag + story filter.  Paginates up to 1000
        results (well above the 500-comment threads we actually see).
        """
        all_comments: list[dict[str, Any]] = []
        per_page = 100
        thread_id_int: int | None
        try:
            thread_id_int = int(thread_id)
        except (ValueError, TypeError):
            thread_id_int = None

        for page in range(10):  # cap at 1000 comments
            resp = await self._fetcher.get_json(
                _ALGOLIA_URL,
                params={
                    "tags": f"comment,story_{thread_id}",
                    "hitsPerPage": str(per_page),
                    "page": str(page),
                },
            )
            hits = resp.data.get("hits") or []
            if not hits:
                break

            for hit in hits:
                # Only keep top-level comments (parent == story root)
                if thread_id_int is None or hit.get("parent_id") == thread_id_int:
                    hit["_thread_id"] = thread_id
                    all_comments.append(hit)

            if len(hits) < per_page:
                break

        return all_comments

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        """Convert one hiring comment to a CanonicalLead.

        Pure function — no I/O.  Top-level comments always get
        SignalType.HIRING (strength 90) regardless of classifier since
        the entire thread is curated hiring posts.  Keyword extraction
        uses the classifier so callers can swap the keyword list.
        """
        comment_html = raw.get("comment_text") or ""
        body = _HTML_TAG.sub("", comment_html).strip()
        if not body:
            return None

        first_line = body.split("\n", 1)[0].strip()
        company_name = _extract_company(first_line)
        company_domain = _extract_domain_from_text(body)
        location = _extract_location(first_line)

        object_id = str(raw.get("objectID", ""))
        url = f"https://news.ycombinator.com/item?id={object_id}"

        return CanonicalLead(
            source="hnhiring",
            source_id=object_id,
            url=url,
            title=first_line[:200] or f"HN Hiring comment {object_id}",
            body=body[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=90,
            company_name=company_name,
            company_domain=company_domain,
            person_name=raw.get("author"),
            location=location,
            keywords=classifier.extract_keywords(body),
            posted_at=_parse_hn_timestamp(raw.get("created_at")),
        )


def _extract_company(first_line: str) -> str | None:
    """Grab the company name from the first line.

    Convention: comments start with "Company | Role | Location" or "Company: ...".
    """
    match = _FIRST_LINE_COMPANY.match(first_line)
    if not match:
        return None
    candidate = match.group(1).strip()
    # Sanity: reject obviously non-company first segments
    if len(candidate) > 80 or len(candidate) < 2:
        return None
    return candidate


def _extract_domain_from_text(text: str) -> str | None:
    """Prefer email domain (usually the company's), fall back to URL domain."""
    email_match = _EMAIL.search(text)
    if email_match:
        domain = email_match.group(1).lower()
        if not _is_generic_email_provider(domain):
            return domain

    url_match = _URL.search(text)
    if url_match:
        domain = url_match.group(1).lower().removeprefix("www.")
        # Skip common platforms — not the company's own domain
        if domain not in _SKIP_DOMAINS:
            return domain

    return None


def _extract_location(first_line: str) -> str | None:
    """Pull a location token from the first line (remote/hybrid/city)."""
    match = _LOCATION_INLINE.search(first_line)
    if not match:
        return None
    return match.group(1).strip()


def _parse_hn_timestamp(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_generic_email_provider(domain: str) -> bool:
    return domain in {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "protonmail.com", "proton.me", "icloud.com", "aol.com",
    }


_SKIP_DOMAINS = {
    "news.ycombinator.com", "ycombinator.com",
    "github.com", "linkedin.com", "twitter.com", "x.com",
    "lever.co", "greenhouse.io", "workable.com", "ashbyhq.com",
    "jobs.lever.co", "boards.greenhouse.io", "apply.workable.com",
}
