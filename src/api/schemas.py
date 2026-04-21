"""Request/response schemas for the scraping API.

Unified across all adapters so n8n workflows can drive any adapter with
the same JSON shape.  Adapters read the fields that apply to them and
fall back to env-configured settings for anything not supplied.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SignalPatternSpec(BaseModel):
    """Per-request signal classification rule.

    A regex that, when matched against a lead's title+body, assigns the
    given signal_type and strength.
    """

    pattern: str = Field(description="Python regex, case-insensitive.")
    signal_type: str = Field(
        description="One of the SignalType enum values (e.g. 'hiring', 'pain_point').",
    )
    strength: int = Field(ge=0, le=100)


class ScrapeRequest(BaseModel):
    """Unified body for POST /scrape/{adapter_name}.

    All fields optional — adapters fall back to env defaults when a field
    is omitted.  Adapters use whichever fields apply to them and ignore
    the rest.
    """

    # What to search for
    queries: list[str] | None = Field(
        default=None,
        description="Search terms. Used by HN, Google CSE, LinkedIn posts, etc.",
    )
    sources: list[str] | None = Field(
        default=None,
        description=(
            "Source identifiers. Reddit subreddits, RSS feed URLs, "
            "Wellfound role slugs, etc."
        ),
    )
    limit: int | None = Field(
        default=None,
        description="Max items per source (adapter-dependent meaning).",
    )

    # Source-specific filters — adapter reads only keys it understands
    filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Source-specific filters. Examples: "
            "{'location': 'Remote'}, {'date_posted': 'past_week'}, "
            "{'job_queries': [...]} for LinkedIn to override job search separately."
        ),
    )

    # Classification overrides — enables non-dev verticals
    signal_patterns: list[SignalPatternSpec] | None = Field(
        default=None,
        description="Custom regex→SignalType rules. If omitted, uses dev-focused defaults.",
    )
    extract_keywords: list[str] | None = Field(
        default=None,
        description=(
            "Keyword list to extract into the lead's `keywords` field. "
            "Replaces default tech stack list."
        ),
    )
    default_signal_type: str | None = Field(
        default=None,
        description="Fallback SignalType when no pattern matches. Requires keep_unclassified=True.",
    )
    default_signal_strength: int = Field(default=50, ge=0, le=100)
    keep_unclassified: bool = Field(
        default=False,
        description=(
            "If True, leads with no signal match are kept (with "
            "default_signal_type). If False, they're dropped."
        ),
    )


class AdapterParamSchema(BaseModel):
    """Describes which ScrapeRequest fields an adapter uses.

    Returned by GET /adapters/{name}/schema so n8n workflow builders
    can auto-fill or validate their requests.
    """

    name: str
    uses_queries: bool = False
    uses_sources: bool = False
    uses_limit: bool = False
    supported_filters: list[str] = Field(default_factory=list)
    default_queries: list[str] = Field(default_factory=list)
    default_sources: list[str] = Field(default_factory=list)
    default_limit: int | None = None
    requires_api_key: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# Prospect discovery schemas (LinkedIn /search-companies & /search-employees)
# ---------------------------------------------------------------------------


class ProspectCompanyDiscoverRequest(BaseModel):
    """Body for POST /prospects/linkedin/companies.

    All fields optional — unset ones fall back to env defaults
    (LEADS_LINKEDIN_COMPANY_*).
    """

    headcounts: list[str] | None = None
    industry_codes: list[int] | None = None
    hq_location_codes: list[int] | None = None
    technologies: list[str] | None = None
    keywords: str | None = None
    headcount_growth_min: int | None = None
    headcount_growth_max: int | None = None
    annual_revenue_min: int | None = None
    annual_revenue_max: int | None = None
    annual_revenue_currency: str | None = None
    hiring_on_linkedin: bool | None = None
    recent_activities: list[str] | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)


class ProspectPeopleDiscoverRequest(BaseModel):
    """Body for POST /prospects/linkedin/employees."""

    seed_urls: list[str] | None = Field(
        default=None,
        description=(
            "Sales Navigator search URLs. Each URL produces up to `limit` people. "
            "Falls back to LEADS_LINKEDIN_EMPLOYEE_SEED_URLS when omitted."
        ),
    )
    limit: int | None = Field(default=None, ge=1, le=100)


class ProspectDiscoveryResult(BaseModel):
    """Response for the two discover endpoints."""

    inserted: int
    duplicates: int
    errors: int = 0
    sample_ids: list[str] = Field(default_factory=list)
