"""Environment-driven configuration via Pydantic Settings.

Every tuneable value is exposed as an env var with a sensible default.
No hardcoded URLs, credentials, or magic strings.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_prefix": "LEADS_", "env_file": ".env", "extra": "ignore"}

    # --- Postgres ---
    database_url: str = Field(
        default="postgresql+asyncpg://leads:leads@localhost:5432/leads",
        description="Async SQLAlchemy connection string.",
    )
    db_pool_size: int = Field(default=10)
    db_max_overflow: int = Field(default=20)
    db_echo: bool = Field(default=False, description="Echo SQL statements for debugging.")

    # --- Redis (kept for caching, no longer used for event streams) ---
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for caching.",
    )

    # --- HTTP fetch layer ---
    http_default_timeout_sec: float = Field(default=30.0)
    http_max_retries: int = Field(default=3)
    http_user_agent_reddit: str = Field(
        default="lead-pipeline/1.0 (by /u/your_username)",
        description="Reddit requires a descriptive User-Agent.",
    )
    http_user_agent_default: str = Field(
        default="lead-pipeline/1.0",
        description="Generic User-Agent for HN, RSS, and other sources.",
    )

    # --- Browser fetch (optional Playwright) ---
    enable_browser_fetcher: bool = Field(
        default=False, description="Set true to enable Playwright-based fetcher."
    )
    browser_restart_after_pages: int = Field(default=100)
    browser_page_timeout_sec: float = Field(default=30.0)
    browser_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )

    # --- Reddit adapter ---
    reddit_subreddits: list[str] = Field(
        default=["startups", "SaaS", "webdev"],
        description="Subreddits to poll for leads.",
    )
    reddit_poll_interval_seconds: int = Field(default=300)
    reddit_post_limit: int = Field(default=25)

    # --- HackerNews adapter ---
    hn_poll_interval_seconds: int = Field(default=600)

    # --- Wellfound adapter (requires browser fetcher) ---
    wellfound_poll_interval_seconds: int = Field(default=3600)
    wellfound_search_roles: list[str] = Field(
        default=["developer", "engineer"],
        description="Wellfound role slugs to scrape.",
    )

    # --- ProductHunt adapter ---
    producthunt_poll_interval_seconds: int = Field(default=21600)

    # --- Generic RSS feeds ---
    rss_feed_urls: list[str] = Field(
        default=[], description="RSS/Atom feed URLs to poll. Empty = adapter disabled.",
    )
    rss_multi_poll_interval_seconds: int = Field(default=1800)

    # --- Google Custom Search Engine ---
    google_cse_api_key: str = Field(
        default="", description="Google CSE API key. Empty = disabled.",
    )
    google_cse_engine_id: str = Field(default="", description="Google CSE search engine ID (cx).")
    google_cse_queries: list[str] = Field(
        default=["site:reddit.com hiring developer", "startup looking for developer"],
    )
    google_cse_poll_interval_seconds: int = Field(default=21600)
    google_cse_daily_query_budget: int = Field(default=100)

    # --- LinkedIn adapter (requires browser fetcher) ---
    linkedin_poll_interval_seconds: int = Field(default=14400)
    linkedin_search_queries: list[str] = Field(
        default=["python developer startup", "fastapi engineer"],
    )
    linkedin_min_delay_sec: float = Field(default=2.0)
    linkedin_max_delay_sec: float = Field(default=5.0)

    # --- Funding sources ---
    funding_feed_urls: list[str] = Field(
        default=[
            "https://techcrunch.com/category/fundraising/feed/",
            "https://news.crunchbase.com/feed/",
        ],
    )
    funding_poll_interval_seconds: int = Field(default=43200)

    # --- Circuit breaker (scraping) ---
    circuit_breaker_threshold: int = Field(
        default=3, description="Consecutive failures before pausing an adapter."
    )
    circuit_breaker_cooldown_seconds: int = Field(
        default=3600, description="Seconds an adapter stays paused after tripping."
    )

    # --- Graceful shutdown ---
    shutdown_timeout_seconds: int = Field(default=30)

    # --- Logging ---
    log_level: str = Field(default="INFO")
    log_json: bool = Field(
        default=True, description="JSON output in production, console in dev."
    )

    # --- LLM provider ---
    llm_provider: str = Field(
        default="anthropic",
        description="Which LLM backend to use: 'anthropic' or 'openai'.",
    )
    anthropic_api_key: str = Field(
        default="", description="Anthropic API key (required when llm_provider=anthropic)."
    )
    llm_model_cheap: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Anthropic model ID for cheap/fast LLM calls.",
    )
    llm_model_smart: str = Field(
        default="claude-sonnet-4-6",
        description="Anthropic model ID for smart/expensive LLM calls.",
    )
    openai_api_key: str = Field(
        default="", description="OpenAI API key (required when llm_provider=openai)."
    )
    openai_model_cheap: str = Field(
        default="gpt-4o-mini",
        description="OpenAI model ID for cheap/fast LLM calls.",
    )
    openai_model_smart: str = Field(
        default="gpt-4o",
        description="OpenAI model ID for smart/expensive LLM calls.",
    )
    daily_llm_budget_usd: float = Field(
        default=10.0, description="Daily LLM spending ceiling in USD."
    )

    # --- Enrichment ---
    enable_enrichment: bool = Field(
        default=False,
        description="Set true to enable the enrichment pipeline and background workers. "
        "Requires a valid LLM API key.",
    )
    max_concurrent_enrichments: int = Field(
        default=5, description="Max concurrent enrichment pipeline runs."
    )
    user_skills: list[str] = Field(
        default=["python", "fastapi", "react", "typescript", "postgres"],
        description="Your tech skills for ICP stack-match scoring.",
    )

    # --- Event bus ---
    event_bus_queue_size: int = Field(
        default=1000, description="Max events buffered per event type."
    )

    # --- Resweeper ---
    resweeper_interval_seconds: int = Field(
        default=300, description="Seconds between resweeper scans."
    )
