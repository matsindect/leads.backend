"""Adapter registry — the single place new adapters are registered.

Adding a new source requires:
1. Create a new file in this package implementing ``SourceAdapter``.
2. Add a factory function and entry to ``ADAPTER_FACTORIES`` below.

No changes to orchestration, storage, or API code are needed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings
    from domain.interfaces import SourceAdapter
    from infrastructure.fetchers.browser import BrowserFetcher
    from infrastructure.fetchers.http import HttpFetcher
    from infrastructure.fetchers.rss import RssFetcher

AdapterFactory = Callable[..., "SourceAdapter | None"]


# ---------------------------------------------------------------------------
# Factory functions — each adapter picks the fetchers it needs
# ---------------------------------------------------------------------------


def _reddit_factory(
    *, rss_fetcher: RssFetcher, settings: Settings, **_: Any,
) -> SourceAdapter:
    from modules.scraping.adapters.reddit import RedditAdapter
    return RedditAdapter(fetcher=rss_fetcher, settings=settings)


def _hackernews_factory(
    *, default_fetcher: HttpFetcher, settings: Settings, **_: Any,
) -> SourceAdapter:
    from modules.scraping.adapters.hackernews import HackerNewsAdapter
    return HackerNewsAdapter(fetcher=default_fetcher, settings=settings)


def _remoteok_factory(
    *, rss_fetcher: RssFetcher, settings: Settings, **_: Any,
) -> SourceAdapter:
    from modules.scraping.adapters.remoteok import RemoteOKAdapter
    return RemoteOKAdapter(fetcher=rss_fetcher, settings=settings)


def _funding_factory(
    *, rss_fetcher: RssFetcher, settings: Settings, **_: Any,
) -> SourceAdapter:
    from modules.scraping.adapters.funding import FundingAdapter
    return FundingAdapter(fetcher=rss_fetcher, settings=settings)


def _producthunt_factory(
    *, rss_fetcher: RssFetcher, settings: Settings, **_: Any,
) -> SourceAdapter:
    from modules.scraping.adapters.producthunt import ProductHuntAdapter
    return ProductHuntAdapter(fetcher=rss_fetcher, settings=settings)


def _rss_multi_factory(
    *, rss_fetcher: RssFetcher, settings: Settings, **_: Any,
) -> SourceAdapter | None:
    if not settings.rss_feed_urls:
        return None  # disabled when no feeds configured
    from modules.scraping.adapters.rss_multi import RssMultiAdapter
    return RssMultiAdapter(fetcher=rss_fetcher, settings=settings)


def _google_cse_factory(
    *, default_fetcher: HttpFetcher, settings: Settings, **_: Any,
) -> SourceAdapter | None:
    if not settings.google_cse_api_key:
        return None  # disabled when no API key
    from modules.scraping.adapters.google_cse import GoogleCSEAdapter
    return GoogleCSEAdapter(fetcher=default_fetcher, settings=settings)


def _wellfound_factory(
    *, browser_fetcher: BrowserFetcher | None, settings: Settings, **_: Any,
) -> SourceAdapter | None:
    if browser_fetcher is None:
        return None  # requires Playwright
    from modules.scraping.adapters.wellfound import WellfoundAdapter
    return WellfoundAdapter(fetcher=browser_fetcher, settings=settings)


def _linkedin_factory(
    *, browser_fetcher: BrowserFetcher | None, settings: Settings, **_: Any,
) -> SourceAdapter | None:
    if browser_fetcher is None:
        return None  # requires Playwright
    from modules.scraping.adapters.linkedin import LinkedInAdapter
    return LinkedInAdapter(fetcher=browser_fetcher, settings=settings)


ADAPTER_FACTORIES: dict[str, AdapterFactory] = {
    "reddit": _reddit_factory,
    "hackernews": _hackernews_factory,
    "remoteok": _remoteok_factory,
    "funding": _funding_factory,
    "producthunt": _producthunt_factory,
    "rss": _rss_multi_factory,
    "google_cse": _google_cse_factory,
    "wellfound": _wellfound_factory,
    "linkedin": _linkedin_factory,
}


def build_adapters(
    *,
    reddit_fetcher: HttpFetcher,
    default_fetcher: HttpFetcher,
    rss_fetcher: RssFetcher,
    browser_fetcher: BrowserFetcher | None = None,
    settings: Settings,
) -> dict[str, SourceAdapter]:
    """Instantiate all registered adapters, skipping those that return None."""
    kwargs: dict[str, Any] = {
        "reddit_fetcher": reddit_fetcher,
        "default_fetcher": default_fetcher,
        "rss_fetcher": rss_fetcher,
        "browser_fetcher": browser_fetcher,
        "settings": settings,
    }
    result: dict[str, SourceAdapter] = {}
    for name, factory in ADAPTER_FACTORIES.items():
        adapter = factory(**kwargs)
        if adapter is not None:
            result[name] = adapter
    return result
