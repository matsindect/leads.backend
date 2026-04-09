"""FastAPI application factory with graceful shutdown.

Entry point: ``uvicorn main:create_app --factory``
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from api.routes import router
from application.workers import start_background_workers
from config import Settings
from container import Container

logger = structlog.get_logger()


def _configure_logging(settings: Settings) -> None:
    """Set up structlog with JSON or console rendering."""
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.get_level_from_name(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup/shutdown of all application resources."""
    settings = Settings()
    _configure_logging(settings)

    container = Container(settings)

    # Wire dependencies into app state
    app.state.settings = container.settings
    app.state.repository = container.repository
    app.state.publisher = container.publisher
    app.state.orchestrator = container.orchestrator
    app.state.adapters = container.adapters
    app.state.event_bus = container.event_bus
    app.state.pipeline = container.pipeline
    app.state.container = container

    # Start background workers only when enrichment is enabled
    worker_tasks: list[asyncio.Task[None]] = []
    if settings.enable_enrichment and container.pipeline is not None:
        worker_tasks = await start_background_workers(
            app=app,
            pipeline=container.pipeline,
            bus=container.event_bus,
            repository=container.repository,
            settings=settings,
        )
    app.state.worker_tasks = worker_tasks

    logger.info(
        "app_started",
        adapters=list(container.adapters.keys()),
        workers=len(worker_tasks),
        enrichment_enabled=settings.enable_enrichment,
    )

    yield

    # Graceful shutdown: cancel workers, wait for in-flight work
    logger.info("shutdown_started")
    for task in worker_tasks:
        task.cancel()

    if worker_tasks:
        _, pending = await asyncio.wait(
            worker_tasks, timeout=settings.shutdown_timeout_seconds
        )
        for task in pending:
            logger.warning("shutdown_force_cancelled", task=task.get_name())

    await container.close()
    logger.info("app_shutdown_complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Lead Pipeline",
        description="Modular monolith: scraping + enrichment in one service.",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.include_router(router, prefix="/api/v1")

    # SIGTERM handler for graceful shutdown in containers
    def _handle_sigterm(signum: int, frame: object) -> None:
        logger.info("sigterm_received")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    return app


# Allow ``uvicorn main:app``
app = create_app()
