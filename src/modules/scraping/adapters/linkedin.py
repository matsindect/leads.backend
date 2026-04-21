"""LinkedIn adapter — jobs, posts, and prospect discovery via RapidAPI.

Uses the Fresh LinkedIn Profile Data host.

Scraping endpoints (emit CanonicalLead):
    POST /search-jobs       body: {"keywords": "...", "page": 1}
    POST /search-jobs-v2    body: {"keywords": "...", "location": "...", "start": 0}
    POST /search-posts      body: {"search_keywords": "...", "sort_by": "Latest", ...}

Prospect-discovery endpoints (emit TargetCompany / TargetPerson):
    POST /search-companies  body: rich filter payload
    POST /search-employees  body: {"url": "<Sales Nav URL>", "limit": 25}

The scraping methods satisfy ``domain.interfaces.SourceAdapter``. The prospect
methods are adapter-specific — they are called by the /prospects routes, not
by the scrape orchestrator, because prospects aren't leads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import (
    CanonicalLead,
    SignalType,
    TargetCompany,
    TargetPerson,
)
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import SignalClassifier

logger = structlog.get_logger()

_BASE_URL = "https://fresh-linkedin-profile-data.p.rapidapi.com"


@dataclass(frozen=True, slots=True)
class CompanySearchParams:
    """Input shape for /search-companies. Mirrors the RapidAPI payload.

    Kept as a plain dataclass (not Pydantic) because it's the adapter's
    internal contract — the API layer builds it from the HTTP request body
    or from Settings defaults.
    """

    headcounts: list[str] = field(default_factory=list)
    industry_codes: list[int] = field(default_factory=list)
    hq_location_codes: list[int] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    keywords: str = ""
    headcount_growth_min: int | None = None
    headcount_growth_max: int | None = None
    annual_revenue_min: int | None = None
    annual_revenue_max: int | None = None
    annual_revenue_currency: str = "USD"
    hiring_on_linkedin: bool = False
    recent_activities: list[str] = field(default_factory=list)
    limit: int = 100


class LinkedInAdapter:
    """Fetches LinkedIn jobs, posts, companies, and employees via RapidAPI."""

    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings
        self._headers = {
            "x-rapidapi-host": settings.linkedin_rapidapi_host,
            "x-rapidapi-key": settings.linkedin_rapidapi_key,
        }

    @property
    def name(self) -> str:
        return "linkedin"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.linkedin_poll_interval_seconds

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            uses_queries=True,
            supported_filters=["job_queries", "post_queries"],
            default_queries=list(self._settings.linkedin_post_queries),
            requires_api_key=True,
            notes=(
                "queries are used as post_queries by default. "
                "filters.job_queries and filters.post_queries override independently. "
                "Uses /search-jobs-v2 when LEADS_LINKEDIN_USE_JOBS_V2=true (default). "
                "Requires LEADS_LINKEDIN_RAPIDAPI_KEY."
            ),
        )

    # ------------------------------------------------------------------
    # SourceAdapter — jobs & posts
    # ------------------------------------------------------------------

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        """Fetch jobs and posts from LinkedIn via RapidAPI."""
        filters = params.filters or {}
        job_queries = (
            filters.get("job_queries")
            or self._settings.linkedin_job_queries
        )
        post_queries = (
            filters.get("post_queries")
            or params.queries
            or self._settings.linkedin_post_queries
        )
        all_items: list[dict[str, Any]] = []

        use_v2 = self._settings.linkedin_use_jobs_v2
        for query in job_queries:
            try:
                jobs = (
                    await self._fetch_jobs_v2(query)
                    if use_v2
                    else await self._fetch_jobs_v1(query)
                )
                for job in jobs:
                    job["_type"] = "job"
                    job["_query"] = query
                all_items.extend(jobs)
                logger.debug(
                    "linkedin_jobs_fetched",
                    query=query, count=len(jobs), endpoint="v2" if use_v2 else "v1",
                )
            except Exception:
                logger.warning(
                    "linkedin_job_search_failed",
                    query=query, exc_info=True,
                )

        for query in post_queries:
            try:
                posts = await self._fetch_posts(query)
                for post in posts:
                    post["_type"] = "post"
                    post["_query"] = query
                all_items.extend(posts)
                logger.debug(
                    "linkedin_posts_fetched",
                    query=query, count=len(posts),
                )
            except Exception:
                logger.warning(
                    "linkedin_post_search_failed",
                    query=query, exc_info=True,
                )

        return all_items

    async def _fetch_jobs_v1(self, query: str) -> list[dict[str, Any]]:
        """POST /search-jobs — legacy endpoint."""
        resp = await self._fetcher.post_json(
            f"{_BASE_URL}/search-jobs",
            json_body={"keywords": query, "page": 1},
            headers=self._headers,
        )
        jobs = resp.data.get("data") or []
        return jobs if isinstance(jobs, list) else []

    async def _fetch_jobs_v2(self, query: str) -> list[dict[str, Any]]:
        """POST /search-jobs-v2 — adds `location` filter and `start` pagination."""
        resp = await self._fetcher.post_json(
            f"{_BASE_URL}/search-jobs-v2",
            json_body={
                "keywords": query,
                "location": self._settings.linkedin_job_location,
                "start": self._settings.linkedin_job_start,
            },
            headers=self._headers,
        )
        jobs = resp.data.get("data") or []
        return jobs if isinstance(jobs, list) else []

    async def _fetch_posts(self, query: str) -> list[dict[str, Any]]:
        """POST /search-posts."""
        resp = await self._fetcher.post_json(
            f"{_BASE_URL}/search-posts",
            json_body={
                "search_keywords": query,
                "sort_by": "Latest",
                "date_posted": "",
                "content_type": "",
                "from_member": [],
                "from_company": [],
                "mentioning_member": [],
                "mentioning_company": [],
                "author_company": [],
                "author_industry": [],
                "author_keyword": "",
                "page": 1,
            },
            headers=self._headers,
        )
        posts = resp.data.get("data") or []
        return posts if isinstance(posts, list) else []

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        """Convert a LinkedIn API result to a CanonicalLead.

        Pure function — no I/O.
        """
        item_type = raw.get("_type", "job")
        if item_type == "post":
            return self._normalize_post(raw, classifier)
        return self._normalize_job(raw, classifier)

    def _normalize_job(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        """Normalize a /search-jobs or /search-jobs-v2 result.

        The v1/v2 responses use overlapping key names — the key-fallback
        chain handles both shapes.
        """
        title = (
            raw.get("job_title")
            or raw.get("title")
            or raw.get("position")
            or ""
        )
        if not title:
            return None

        company = raw.get("company") or raw.get("company_name") or ""
        location = raw.get("location") or raw.get("job_location") or ""
        url = (
            raw.get("job_url")
            or raw.get("url")
            or raw.get("apply_url")
            or ""
        )
        description = raw.get("description") or raw.get("job_description") or ""

        combined = f"{title} {company} {description}"

        return CanonicalLead(
            source="linkedin",
            source_id=str(raw.get("job_id") or raw.get("id") or url),
            url=url,
            title=f"{company} — {title}" if company else title,
            body=description[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=80,
            company_name=company or None,
            location=location or None,
            keywords=classifier.extract_keywords(combined),
            posted_at=_parse_date(
                raw.get("posted_date") or raw.get("posted_at") or raw.get("posted")
            ),
        )

    def _normalize_post(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        """Normalize a /search-posts result."""
        text = raw.get("text") or raw.get("content") or ""
        if not text:
            return None

        signal_type, signal_strength = classifier.classify(text)
        if signal_type is None:
            return None

        author = raw.get("poster_name") or raw.get("author_name") or ""
        author_title = raw.get("poster_title") or ""
        url = raw.get("post_url") or raw.get("url") or ""

        return CanonicalLead(
            source="linkedin",
            source_id=str(raw.get("post_id") or url),
            url=url,
            title=text[:120],
            body=text[:5000],
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            person_name=author or None,
            person_role=author_title or None,
            keywords=classifier.extract_keywords(text),
            posted_at=_parse_date(raw.get("posted") or raw.get("posted_at")),
        )

    # ------------------------------------------------------------------
    # Prospect discovery — companies & employees
    # ------------------------------------------------------------------

    async def fetch_target_companies(
        self, params: CompanySearchParams
    ) -> list[TargetCompany]:
        """POST /search-companies and normalize into TargetCompany objects.

        Exceptions propagate — the caller decides how to surface them.
        """
        body = _build_company_search_body(params)
        resp = await self._fetcher.post_json(
            f"{_BASE_URL}/search-companies",
            json_body=body,
            headers=self._headers,
        )
        raw_list = resp.data.get("data") or []
        if not isinstance(raw_list, list):
            return []

        results: list[TargetCompany] = []
        for raw in raw_list:
            try:
                results.append(_to_target_company(raw))
            except Exception:
                logger.warning("linkedin_company_normalize_error", exc_info=True)
        logger.info("linkedin_companies_fetched", count=len(results))
        return results

    async def fetch_target_people(
        self, seed_urls: list[str], limit: int
    ) -> list[TargetPerson]:
        """POST /search-employees once per seed URL, return TargetPerson list."""
        results: list[TargetPerson] = []
        for url in seed_urls:
            try:
                resp = await self._fetcher.post_json(
                    f"{_BASE_URL}/search-employees",
                    json_body={"url": url, "limit": limit},
                    headers=self._headers,
                )
                raw_list = resp.data.get("data") or []
                if not isinstance(raw_list, list):
                    continue
                for raw in raw_list:
                    try:
                        results.append(_to_target_person(raw, seed_url=url))
                    except Exception:
                        logger.warning(
                            "linkedin_person_normalize_error", exc_info=True
                        )
            except Exception:
                logger.warning(
                    "linkedin_employee_search_failed",
                    seed_url=url, exc_info=True,
                )
        logger.info("linkedin_people_fetched", count=len(results))
        return results


# ---------------------------------------------------------------------------
# Pure helpers — body builders and normalizers
# ---------------------------------------------------------------------------


def _build_company_search_body(params: CompanySearchParams) -> dict[str, Any]:
    """Construct the /search-companies JSON body from typed params.

    Optional ranges (revenue, growth) are only included when both bounds
    are set — partial ranges would be rejected by the API.
    """
    body: dict[str, Any] = {
        "company_headcounts": params.headcounts,
        "industry_codes": params.industry_codes,
        "headquarters_location": params.hq_location_codes,
        "technologies_used": params.technologies,
        "keywords": params.keywords,
        "hiring_on_linkedin": "true" if params.hiring_on_linkedin else "false",
        "recent_activities": params.recent_activities,
        "limit": params.limit,
    }
    if (
        params.annual_revenue_min is not None
        and params.annual_revenue_max is not None
    ):
        body["annual_revenue"] = {
            "min": params.annual_revenue_min,
            "max": params.annual_revenue_max,
            "currency": params.annual_revenue_currency,
        }
    if (
        params.headcount_growth_min is not None
        and params.headcount_growth_max is not None
    ):
        body["company_headcount_growth"] = {
            "min": params.headcount_growth_min,
            "max": params.headcount_growth_max,
        }
    return body


def _to_target_company(raw: dict[str, Any]) -> TargetCompany:
    """Normalize a /search-companies result into a TargetCompany."""
    name = raw.get("name") or raw.get("company_name") or ""
    if not name:
        raise ValueError("missing company name")

    source_id = str(
        raw.get("company_id")
        or raw.get("id")
        or raw.get("linkedin_url")
        or name
    )

    growth = raw.get("headcount_growth")
    revenue = raw.get("annual_revenue") if isinstance(raw.get("annual_revenue"), dict) else {}
    rev_min = revenue.get("min") if revenue else None
    rev_max = revenue.get("max") if revenue else None

    return TargetCompany(
        source="linkedin",
        source_id=source_id,
        name=name,
        raw_payload=raw,
        linkedin_url=raw.get("linkedin_url") or raw.get("url"),
        domain=raw.get("website") or raw.get("domain"),
        industry=raw.get("industry"),
        headcount_band=raw.get("headcount") or raw.get("company_size"),
        headcount_growth=int(growth) if isinstance(growth, (int, float)) else None,
        annual_revenue_min=int(rev_min) if rev_min is not None else None,
        annual_revenue_max=int(rev_max) if rev_max is not None else None,
        annual_revenue_currency=revenue.get("currency") if revenue else None,
        hq_location=raw.get("headquarters") or raw.get("location"),
        technologies=list(raw.get("technologies_used") or raw.get("technologies") or []),
        hiring_on_linkedin=raw.get("hiring_on_linkedin"),
    )


def _to_target_person(raw: dict[str, Any], *, seed_url: str) -> TargetPerson:
    """Normalize a /search-employees result into a TargetPerson."""
    full_name = (
        raw.get("full_name")
        or raw.get("name")
        or " ".join(filter(None, [raw.get("first_name"), raw.get("last_name")]))
        or ""
    )
    if not full_name:
        raise ValueError("missing person name")

    source_id = str(
        raw.get("profile_id")
        or raw.get("public_id")
        or raw.get("id")
        or raw.get("profile_url")
        or raw.get("linkedin_url")
        or full_name
    )

    return TargetPerson(
        source="linkedin",
        source_id=source_id,
        full_name=full_name,
        raw_payload=raw,
        linkedin_url=raw.get("profile_url") or raw.get("linkedin_url"),
        headline=raw.get("headline"),
        current_title=raw.get("current_title") or raw.get("title"),
        current_company=raw.get("current_company") or raw.get("company"),
        current_company_domain=raw.get("current_company_domain") or raw.get("company_domain"),
        location=raw.get("location"),
        seed_url=seed_url,
    )


def _parse_date(date_val: str | None) -> datetime | None:
    """Parse date from LinkedIn API — ISO string or 'YYYY-MM-DD HH:MM:SS.fff'."""
    if not date_val:
        return None
    # Try ISO-with-Z first
    try:
        return datetime.fromisoformat(date_val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    # Try LinkedIn's "2026-04-21 06:56:57.000" format
    try:
        return datetime.strptime(date_val, "%Y-%m-%d %H:%M:%S.%f")
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(date_val, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
