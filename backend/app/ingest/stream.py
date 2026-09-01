"""Market data stream client with reconnect, backoff, and heartbeat watchdog.

BUILD_SPEC §7.1.

The stream is abstracted behind `MessageSource` so the identical downstream
pipeline runs against either the live Alpaca websocket or a recorded file. The
replay path is what makes this system testable outside market hours, which is
most of the time.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TradeMsg:
    symbol: str
    ts: datetime
    price: Decimal
    size: int


@dataclass(frozen=True)
class QuoteMsg:
    symbol: str
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_size: int = 0
    ask_size: int = 0

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class BarMsg:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None
    trade_count: int | None = None


Message = TradeMsg | QuoteMsg | BarMsg


class MessageSource(Protocol):
    """Anything that can yield market data messages."""

    def stream(self, symbols: list[str]) -> AsyncIterator[Message]: ...


class BackoffPolicy:
    """Exponential backoff with full jitter, capped.

    Full jitter (rather than a fixed multiplier) matters: if the socket drops
    because Alpaca restarted, every client reconnecting on the same schedule
    produces a thundering herd.
    """

    def __init__(
        self,
        base_seconds: float = 1.0,
        max_seconds: float = 30.0,
        rng: random.Random | None = None,
    ):
        self.base = base_seconds
        self.max = max_seconds
        self._attempt = 0
        self._rng = rng or random.Random()

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        self._attempt += 1
        ceiling = min(self.max, self.base * (2 ** (self._attempt - 1)))
        return self._rng.uniform(0.0, ceiling)

    @property
    def attempts(self) -> int:
        return self._attempt


class StreamRunner:
    """Owns the connection lifecycle. Single instance per deployment.

    A second instance would duplicate the socket and race on bar building, which
    is why `ingest` runs as its own single-instance process (BUILD_SPEC §4).
    """

    def __init__(
        self,
        source: MessageSource,
        on_message: Callable[[Message], object],
        heartbeat_timeout: float = 60.0,
        backoff: BackoffPolicy | None = None,
        on_reconnect: Callable[[], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.source = source
        self.on_message = on_message
        self.heartbeat_timeout = heartbeat_timeout
        self.backoff = backoff or BackoffPolicy()
        self.on_reconnect = on_reconnect
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_message_at: datetime | None = None
        self._running = False
        self._stopped = asyncio.Event()

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    def is_stale(self, now: datetime | None = None) -> bool:
        """True if no message has arrived within the heartbeat window."""
        if self._last_message_at is None:
            return False
        now = now or self._clock()
        return (now - self._last_message_at).total_seconds() > self.heartbeat_timeout

    async def run(self, symbols: list[str], max_reconnects: int | None = None) -> None:
        """Consume forever, reconnecting on failure.

        `max_reconnects` exists for tests; leave it None in production.
        """
        self._running = True
        reconnects = 0

        while self._running:
            try:
                log.info("stream.connecting", symbols=len(symbols),
                         attempt=self.backoff.attempts + 1)
                async for msg in self.source.stream(symbols):
                    if not self._running:
                        break
                    self._last_message_at = self._clock()
                    self.backoff.reset()
                    self.on_message(msg)
                # Clean end of stream (replay finished, or server closed).
                if not self._running:
                    break
                log.warning("stream.ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                log.error("stream.error", error=str(exc), exc_type=type(exc).__name__)

            if not self._running:
                break

            reconnects += 1
            if max_reconnects is not None and reconnects > max_reconnects:
                break

            delay = self.backoff.next_delay()
            log.info("stream.reconnecting", delay_seconds=round(delay, 2))
            await asyncio.sleep(delay)

            if self.on_reconnect is not None:
                # Gap backfill hooks in here. BUILD_SPEC §7.1: never leave a hole.
                result = self.on_reconnect()
                if asyncio.iscoroutine(result):
                    await result

        self._stopped.set()

    def stop(self) -> None:
        self._running = False

    async def wait_stopped(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
