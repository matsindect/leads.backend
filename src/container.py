"""Dependency wiring — creates and connects all collaborators at startup.

Uses FastAPI's native DI via app.state rather than the dependency-injector
library, keeping the wiring simple and transparent.  All I/O clients are
created once here and injected into the layers that need them.
"""

from __future__ import annotations

import httpx
import redis.asyncio as aioredis

from application.bus import EventBus
from config import Settings
from infrastructure.anthropic_provider import AnthropicProvider
from infrastructure.bus_publisher import BusEventPublisher
from infrastructure.db import create_engine, create_session_factory
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher
from infrastructure.openai_provider import OpenAIProvider
from infrastructure.postgres_repo import PostgresLeadRepository
from infrastructure.prompt_loader import PromptLoader
from modules.enrichment.company_resolver import LLMCompanyResolver
from modules.enrichment.pipeline import EnrichmentPipeline
from modules.scraping.adapters import build_adapters
from modules.scraping.orchestrator import ScrapeOrchestrator


class Container:
    """Holds all application-scoped resources.

    Created once by the app factory and attached to ``app.state``.
    Call ``close()`` during shutdown to release connections cleanly.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        # I/O clients
        self.engine = create_engine(settings)
        self.session_factory = create_session_factory(self.engine)
        self.redis_client = aioredis.from_url(
            settings.redis_url, decode_responses=True
        )

        # Shared HTTP client (connection pooling, HTTP/2)
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_default_timeout_sec),
            follow_redirects=True,
            http2=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

        # Event bus (in-process, replaces Redis Streams)
        self.event_bus = EventBus(max_queue_size=settings.event_bus_queue_size)

        # Infrastructure
        self.repository = PostgresLeadRepository(self.session_factory)
        self.publisher = BusEventPublisher(self.event_bus)
        self.prompt_loader = PromptLoader()

        # Fetchers — one per distinct User-Agent requirement
        self.reddit_fetcher = HttpFetcher(
            self.http_client,
            user_agent=settings.http_user_agent_reddit,
            default_timeout_sec=settings.http_default_timeout_sec,
            max_retries=settings.http_max_retries,
        )
        self.default_fetcher = HttpFetcher(
            self.http_client,
            user_agent=settings.http_user_agent_default,
            default_timeout_sec=settings.http_default_timeout_sec,
            max_retries=settings.http_max_retries,
        )
        self.rss_fetcher = RssFetcher(self.default_fetcher)

        # Browser fetcher (optional — only when enabled)
        self.browser_pool = None
        self.browser_fetcher = None
        if settings.enable_browser_fetcher:
            from infrastructure.fetchers.browser import BrowserFetcher, BrowserPool

            self.browser_pool = BrowserPool(
                restart_after_pages=settings.browser_restart_after_pages,
                page_timeout_sec=settings.browser_page_timeout_sec,
                user_agent=settings.browser_user_agent,
            )
            self.browser_fetcher = BrowserFetcher(self.browser_pool)

        # Enrichment (only when enabled — requires a valid LLM API key)
        self.llm_provider = None
        self.company_resolver = None
        self.pipeline = None

        if settings.enable_enrichment:
            if settings.llm_provider == "openai":
                self.llm_provider = OpenAIProvider(settings)
            else:
                self.llm_provider = AnthropicProvider(settings)

            self.company_resolver = LLMCompanyResolver(
                llm=self.llm_provider,
                repository=self.repository,
                prompt_loader=self.prompt_loader,
            )

            self.pipeline = EnrichmentPipeline(
                repository=self.repository,
                llm=self.llm_provider,
                resolver=self.company_resolver,
                prompt_loader=self.prompt_loader,
                http_client=self.http_client,
                event_bus=self.event_bus,
                settings=settings,
            )

        # Scraping module
        self.orchestrator = ScrapeOrchestrator(
            repository=self.repository,
            publisher=self.publisher,
            settings=settings,
        )
        self.adapters = build_adapters(
            reddit_fetcher=self.reddit_fetcher,
            default_fetcher=self.default_fetcher,
            rss_fetcher=self.rss_fetcher,
            browser_fetcher=self.browser_fetcher,
            settings=settings,
        )

    async def close(self) -> None:
        """Release all connections gracefully."""
        if self.browser_pool is not None:
            await self.browser_pool.close()
        await self.http_client.aclose()
        await self.redis_client.aclose()
        await self.engine.dispose()
