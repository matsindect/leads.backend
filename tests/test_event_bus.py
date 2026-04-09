"""Unit tests for the in-process EventBus."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from application.bus import EventBus


@dataclass(frozen=True)
class FakeEvent:
    value: int


@dataclass(frozen=True)
class OtherEvent:
    name: str


class TestEventBus:
    """Verify publish/consume semantics."""

    @pytest.mark.asyncio
    async def test_publish_and_consume(self) -> None:
        """Published event is received by the consumer."""
        bus = EventBus(max_queue_size=10)
        received: list[FakeEvent] = []

        async def handler(event: FakeEvent) -> None:
            received.append(event)

        # Start consumer in background
        task = asyncio.create_task(
            bus.consume(FakeEvent, handler, worker_name="test")
        )

        await bus.publish(FakeEvent(value=42))
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].value == 42

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_different_event_types_isolated(self) -> None:
        """Events of different types go to separate queues."""
        bus = EventBus(max_queue_size=10)
        fake_received: list[FakeEvent] = []
        other_received: list[OtherEvent] = []

        task1 = asyncio.create_task(
            bus.consume(FakeEvent, lambda e: _append(fake_received, e), worker_name="t1")
        )
        task2 = asyncio.create_task(
            bus.consume(OtherEvent, lambda e: _append(other_received, e), worker_name="t2")
        )

        await bus.publish(FakeEvent(value=1))
        await bus.publish(OtherEvent(name="hello"))
        await asyncio.sleep(0.05)

        assert len(fake_received) == 1
        assert len(other_received) == 1

        task1.cancel()
        task2.cancel()

    @pytest.mark.asyncio
    async def test_queue_full_drops_event(self) -> None:
        """When queue is full, publish drops the event without raising."""
        bus = EventBus(max_queue_size=2)

        # Fill the queue without a consumer
        await bus.publish(FakeEvent(value=1))
        await bus.publish(FakeEvent(value=2))
        # This should be dropped, not raise
        await bus.publish(FakeEvent(value=3))

        assert bus.queue_size(FakeEvent) == 2

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_consumer(self) -> None:
        """A failing handler logs but doesn't stop the consume loop."""
        bus = EventBus(max_queue_size=10)
        received: list[int] = []

        async def flaky_handler(event: FakeEvent) -> None:
            if event.value == 1:
                raise ValueError("boom")
            received.append(event.value)

        task = asyncio.create_task(
            bus.consume(FakeEvent, flaky_handler, worker_name="flaky")
        )

        await bus.publish(FakeEvent(value=1))  # will fail
        await bus.publish(FakeEvent(value=2))  # should succeed
        await asyncio.sleep(0.05)

        assert received == [2]

        task.cancel()

    @pytest.mark.asyncio
    async def test_queue_size(self) -> None:
        """queue_size returns 0 for unknown types and actual size otherwise."""
        bus = EventBus(max_queue_size=10)
        assert bus.queue_size(FakeEvent) == 0

        await bus.publish(FakeEvent(value=1))
        assert bus.queue_size(FakeEvent) == 1


async def _append(lst: list, item: object) -> None:
    lst.append(item)
