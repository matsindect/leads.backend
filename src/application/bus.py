"""In-process event bus backed by asyncio.Queue.

One queue per event type (lazy-created).  This replaces Redis Streams for
inter-module communication.  Postgres status column is the durable queue —
the bus is just the fast path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger()

EventHandler = Callable[[Any], Awaitable[None]]


class EventBus:
    """Publish/subscribe event bus using per-type asyncio.Queues.

    Singleton — created once in the DI container and shared across modules.
    """

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._max_queue_size = max_queue_size
        self._queues: dict[type, asyncio.Queue[Any]] = {}

    def _get_queue(self, event_type: type) -> asyncio.Queue[Any]:
        """Lazily create a bounded queue for the given event type."""
        if event_type not in self._queues:
            self._queues[event_type] = asyncio.Queue(maxsize=self._max_queue_size)
        return self._queues[event_type]

    async def publish(self, event: object) -> None:
        """Non-blocking publish. Logs and drops if queue is full."""
        queue = self._get_queue(type(event))
        try:
            queue.put_nowait(event)
            logger.debug("event_published", event_type=type(event).__name__)
        except asyncio.QueueFull:
            # Dropped events are recovered by the PendingLeadsResweeper
            logger.warning(
                "event_dropped_queue_full",
                event_type=type(event).__name__,
                queue_size=queue.qsize(),
            )

    async def consume(
        self,
        event_type: type,
        handler: EventHandler,
        *,
        worker_name: str,
    ) -> None:
        """Long-running coroutine that pulls events and invokes the handler.

        Handler exceptions are logged but never crash the loop.
        Calls task_done() after each event so queue.join() works for shutdown.
        """
        queue = self._get_queue(event_type)
        log = logger.bind(worker=worker_name, event_type=event_type.__name__)
        log.info("consumer_started")

        while True:
            event = await queue.get()
            try:
                await handler(event)
            except asyncio.CancelledError:
                queue.task_done()
                raise
            except Exception:
                log.error(
                    "handler_error",
                    event=repr(event),
                    exc_info=True,
                )
            finally:
                queue.task_done()

    def queue_size(self, event_type: type) -> int:
        """Return current queue depth for an event type."""
        if event_type not in self._queues:
            return 0
        return self._queues[event_type].qsize()
