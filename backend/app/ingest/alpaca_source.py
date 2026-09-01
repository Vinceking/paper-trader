"""Alpaca websocket adapter implementing MessageSource.

alpaca-py's StockDataStream is callback-based; this bridges it to an async
iterator via a bounded queue so the rest of the pipeline never knows the
difference between live and replay.

Free tier notes (BUILD_SPEC §2.3, §2.4):
  - feed='iex' is the only real-time option; it is a PARTIAL tape, not the SIP
    consolidated feed. Fill prices derived from it are approximations.
  - concurrent symbol subscriptions are capped (currently 30).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal

import structlog

from app.ingest.stream import BarMsg, Message, QuoteMsg, TradeMsg

log = structlog.get_logger(__name__)

_QUEUE_MAX = 10_000


def _as_utc(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    # alpaca-py may hand back nanosecond ints depending on version
    return datetime.fromtimestamp(int(ts) / 1e9, tz=timezone.utc)


def _dec(v) -> Decimal:
    return Decimal(str(v))


class AlpacaSource:
    def __init__(self, api_key: str, api_secret: str, feed: str = "iex"):
        if not api_key or not api_secret:
            raise ValueError(
                "Alpaca credentials missing. Create a paper account at "
                "alpaca.markets (email only, no funding required) and set "
                "ALPACA_API_KEY / ALPACA_API_SECRET."
            )
        self.api_key = api_key
        self.api_secret = api_secret
        self.feed = feed
        self._queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """Messages discarded because the consumer fell behind. Should stay 0."""
        return self._dropped

    def _put(self, msg: Message) -> None:
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Drop rather than block the websocket reader — a blocked reader
            # stalls the socket and gets us disconnected.
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("alpaca.queue_full", dropped=self._dropped)

    async def stream(self, symbols: list[str]) -> AsyncIterator[Message]:
        from alpaca.data.live import StockDataStream

        stream = StockDataStream(self.api_key, self.api_secret, feed=self.feed)

        async def on_trade(t):
            self._put(TradeMsg(t.symbol, _as_utc(t.timestamp), _dec(t.price), int(t.size)))

        async def on_quote(q):
            self._put(
                QuoteMsg(
                    q.symbol, _as_utc(q.timestamp),
                    _dec(q.bid_price), _dec(q.ask_price),
                    int(getattr(q, "bid_size", 0) or 0),
                    int(getattr(q, "ask_size", 0) or 0),
                )
            )

        async def on_bar(b):
            self._put(
                BarMsg(
                    b.symbol, _as_utc(b.timestamp),
                    _dec(b.open), _dec(b.high), _dec(b.low), _dec(b.close),
                    int(b.volume),
                    _dec(b.vwap) if getattr(b, "vwap", None) is not None else None,
                    getattr(b, "trade_count", None),
                )
            )

        stream.subscribe_trades(on_trade, *symbols)
        stream.subscribe_quotes(on_quote, *symbols)
        stream.subscribe_bars(on_bar, *symbols)

        runner = asyncio.create_task(stream._run_forever())
        try:
            while True:
                msg = await self._queue.get()
                yield msg
        finally:
            runner.cancel()
            try:
                await stream.close()
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass
