"""Shared httpx.AsyncClient factory.

A single client is created at startup so all adapters share the same
connection pool and timeout defaults.
"""

from __future__ import annotations

import httpx

from config import Settings


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    """Build a reusable async HTTP client with sensible defaults."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
