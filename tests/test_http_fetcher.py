"""Unit tests for HttpFetcher using respx to mock httpx."""

from __future__ import annotations

import httpx
import pytest
import respx

from infrastructure.fetchers.base import (
    PermanentFetcherError,
    RateLimitedError,
    TransientFetcherError,
)
from infrastructure.fetchers.http import HttpFetcher


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@pytest.fixture
def fetcher(client: httpx.AsyncClient) -> HttpFetcher:
    return HttpFetcher(client, user_agent="test-agent/1.0", max_retries=3)


class TestGetJson:
    """Verify JSON fetching happy path and error cases."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_success(self, fetcher: HttpFetcher) -> None:
        """Successful JSON response returns parsed data."""
        respx.get("https://api.example.com/data").respond(
            200, json={"key": "value"}
        )
        resp = await fetcher.get_json("https://api.example.com/data")

        assert resp.status == 200
        assert resp.data == {"key": "value"}
        assert resp.duration_ms >= 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_with_params(self, fetcher: HttpFetcher) -> None:
        """Query params are forwarded."""
        route = respx.get("https://api.example.com/data").respond(
            200, json={"ok": True}
        )
        await fetcher.get_json("https://api.example.com/data", params={"limit": "10"})

        assert route.called
        assert "limit=10" in str(route.calls[0].request.url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_user_agent_header(self, fetcher: HttpFetcher) -> None:
        """Configured User-Agent is sent."""
        route = respx.get("https://api.example.com/data").respond(200, json={})
        await fetcher.get_json("https://api.example.com/data")

        assert route.calls[0].request.headers["user-agent"] == "test-agent/1.0"


class TestRetryOn5xx:
    """Verify retry behavior on server errors."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_5xx_retry_then_success(self, client: httpx.AsyncClient) -> None:
        """500 on first attempt, 200 on second — succeeds."""
        fetcher = HttpFetcher(client, user_agent="test", max_retries=3)
        route = respx.get("https://api.example.com/data")
        route.side_effect = [
            httpx.Response(500),
            httpx.Response(200, json={"ok": True}),
        ]

        resp = await fetcher.get_json("https://api.example.com/data")
        assert resp.status == 200
        assert resp.data == {"ok": True}

    @respx.mock
    @pytest.mark.asyncio
    async def test_5xx_retries_exhausted(self, client: httpx.AsyncClient) -> None:
        """500 on every attempt raises TransientFetcherError."""
        fetcher = HttpFetcher(client, user_agent="test", max_retries=2)
        respx.get("https://api.example.com/data").respond(502)

        with pytest.raises(TransientFetcherError) as exc_info:
            await fetcher.get_json("https://api.example.com/data")

        assert exc_info.value.status == 502
        assert "api.example.com" in exc_info.value.url


class TestRateLimit429:
    """Verify 429 handling with Retry-After."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_retry_then_success(self, client: httpx.AsyncClient) -> None:
        """429 on first attempt with Retry-After, then 200."""
        fetcher = HttpFetcher(client, user_agent="test", max_retries=3)
        route = respx.get("https://api.example.com/data")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]

        resp = await fetcher.get_json("https://api.example.com/data")
        assert resp.status == 200

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_retries_exhausted(self, client: httpx.AsyncClient) -> None:
        """429 on every attempt raises RateLimitedError."""
        fetcher = HttpFetcher(client, user_agent="test", max_retries=2)
        respx.get("https://api.example.com/data").respond(
            429, headers={"Retry-After": "0"}
        )

        with pytest.raises(RateLimitedError):
            await fetcher.get_json("https://api.example.com/data")


class TestPermanentErrors:
    """Verify 4xx (non-429) raises immediately without retry."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_404_raises_permanent(self, fetcher: HttpFetcher) -> None:
        route = respx.get("https://api.example.com/data").respond(404)

        with pytest.raises(PermanentFetcherError) as exc_info:
            await fetcher.get_json("https://api.example.com/data")

        assert exc_info.value.status == 404
        # Should NOT have retried — only one call
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_403_raises_permanent(self, fetcher: HttpFetcher) -> None:
        respx.get("https://api.example.com/data").respond(403)

        with pytest.raises(PermanentFetcherError):
            await fetcher.get_json("https://api.example.com/data")


class TestNetworkErrors:
    """Verify retry on transport errors."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error_retry_then_success(self, client: httpx.AsyncClient) -> None:
        fetcher = HttpFetcher(client, user_agent="test", max_retries=3)
        route = respx.get("https://api.example.com/data")
        route.side_effect = [
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json={"ok": True}),
        ]

        resp = await fetcher.get_json("https://api.example.com/data")
        assert resp.status == 200

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error_exhausted(self, client: httpx.AsyncClient) -> None:
        fetcher = HttpFetcher(client, user_agent="test", max_retries=2)
        respx.get("https://api.example.com/data").mock(
            side_effect=httpx.ConnectError("refused")
        )

        with pytest.raises(TransientFetcherError, match="Network error"):
            await fetcher.get_json("https://api.example.com/data")


class TestRateLimitHeaderParsing:
    """Verify rate-limit header extraction."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_standard_headers(self, fetcher: HttpFetcher) -> None:
        respx.get("https://api.example.com/data").respond(
            200,
            json={},
            headers={
                "X-RateLimit-Remaining": "42",
                "X-RateLimit-Reset": "1700000000",
                "X-RateLimit-Limit": "100",
            },
        )

        resp = await fetcher.get_json("https://api.example.com/data")
        assert resp.rate_limit is not None
        assert resp.rate_limit.remaining == 42
        assert resp.rate_limit.limit == 100
        assert resp.rate_limit.reset_at is not None

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_rate_limit_headers(self, fetcher: HttpFetcher) -> None:
        respx.get("https://api.example.com/data").respond(200, json={})

        resp = await fetcher.get_json("https://api.example.com/data")
        assert resp.rate_limit is None


class TestGetText:
    """Verify raw text fetching."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_raw_text(self, fetcher: HttpFetcher) -> None:
        respx.get("https://example.com/feed.xml").respond(
            200, text="<rss>xml content</rss>"
        )

        resp = await fetcher.get_text("https://example.com/feed.xml")
        assert resp.status == 200
        assert "<rss>" in resp.data


class TestPostJson:
    """Verify POST with JSON body."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_post_json_success(self, fetcher: HttpFetcher) -> None:
        route = respx.post("https://api.example.com/graphql").respond(
            200, json={"data": {"posts": []}}
        )

        resp = await fetcher.post_json(
            "https://api.example.com/graphql",
            json_body={"query": "{ posts { title } }"},
        )

        assert resp.status == 200
        assert resp.data == {"data": {"posts": []}}
        assert route.calls[0].request.headers["user-agent"] == "test-agent/1.0"
