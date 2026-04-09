"""FastAPI dependency injection providers.

All dependencies are wired at startup via the app's ``state`` object — no
global singletons or service locator pattern.  Route functions receive
fully constructed collaborators through ``Depends()``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from application.bus import EventBus
from config import Settings
from domain.interfaces import EnrichmentRepository, EventPublisher, LeadRepository, SourceAdapter
from modules.enrichment.pipeline import EnrichmentPipeline
from modules.scraping.orchestrator import ScrapeOrchestrator


def get_settings(request: Request) -> Settings:
    """Retrieve settings from app state."""
    return request.app.state.settings  # type: ignore[no-any-return]


def get_repository(request: Request) -> LeadRepository:
    """Retrieve the lead repository from app state."""
    return request.app.state.repository  # type: ignore[no-any-return]


def get_enrichment_repository(request: Request) -> EnrichmentRepository:
    """Retrieve the enrichment repository from app state."""
    return request.app.state.repository  # type: ignore[no-any-return]


def get_publisher(request: Request) -> EventPublisher:
    """Retrieve the event publisher from app state."""
    return request.app.state.publisher  # type: ignore[no-any-return]


def get_orchestrator(request: Request) -> ScrapeOrchestrator:
    """Retrieve the scrape orchestrator from app state."""
    return request.app.state.orchestrator  # type: ignore[no-any-return]


def get_adapters(request: Request) -> dict[str, SourceAdapter]:
    """Retrieve the adapter registry from app state."""
    return request.app.state.adapters  # type: ignore[no-any-return]


def get_pipeline(request: Request) -> EnrichmentPipeline:
    """Retrieve the enrichment pipeline from app state."""
    return request.app.state.pipeline  # type: ignore[no-any-return]


def get_event_bus(request: Request) -> EventBus:
    """Retrieve the event bus from app state."""
    return request.app.state.event_bus  # type: ignore[no-any-return]


# Annotated aliases for cleaner route signatures
SettingsDep = Annotated[Settings, Depends(get_settings)]
RepositoryDep = Annotated[LeadRepository, Depends(get_repository)]
EnrichmentRepoDep = Annotated[EnrichmentRepository, Depends(get_enrichment_repository)]
PublisherDep = Annotated[EventPublisher, Depends(get_publisher)]
OrchestratorDep = Annotated[ScrapeOrchestrator, Depends(get_orchestrator)]
AdaptersDep = Annotated[dict[str, SourceAdapter], Depends(get_adapters)]
PipelineDep = Annotated[EnrichmentPipeline, Depends(get_pipeline)]
EventBusDep = Annotated[EventBus, Depends(get_event_bus)]
