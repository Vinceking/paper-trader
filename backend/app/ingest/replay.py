"""Record/replay harness.

BUILD_SPEC §18: "Build this in Phase 1 — it will save you more time than
anything else in this document."

Markets are open 6.5 hours a day, five days a week. Without this you can only
test roughly 20% of the time, and never deterministically. With it you record one
real day and replay it as often as you like, at any speed, offline.

File format: newline-delimited JSON, one message per line, in arrival order.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import structlog

from app.ingest.stream import BarMsg, Message, QuoteMsg, TradeMsg

log = structlog.get_logger(__name__)


def _encode(msg: Message) -> dict:
    if isinstance(msg, TradeMsg):
        return {
            "t": "trade", "symbol": msg.symbol, "ts": msg.ts.isoformat(),
            "price": str(msg.price), "size": msg.size,
        }
    if isinstance(msg, QuoteMsg):
        return {
            "t": "quote", "symbol": msg.symbol, "ts": msg.ts.isoformat(),
            "bid": str(msg.bid), "ask": str(msg.ask),
            "bid_size": msg.bid_size, "ask_size": msg.ask_size,
        }
    if isinstance(msg, BarMsg):
        return {
            "t": "bar", "symbol": msg.symbol, "ts": msg.ts.isoformat(),
            "open": str(msg.open), "high": str(msg.high), "low": str(msg.low),
            "close": str(msg.close), "volume": msg.volume,
            "vwap": str(msg.vwap) if msg.vwap is not None else None,
            "trade_count": msg.trade_count,
        }
    raise TypeError(f"cannot encode {type(msg).__name__}")


def _decode(d: dict) -> Message:
    kind = d["t"]
    ts = datetime.fromisoformat(d["ts"])
    if kind == "trade":
        return TradeMsg(d["symbol"], ts, Decimal(d["price"]), int(d["size"]))
    if kind == "quote":
        return QuoteMsg(
            d["symbol"], ts, Decimal(d["bid"]), Decimal(d["ask"]),
            int(d.get("bid_size", 0)), int(d.get("ask_size", 0)),
        )
    if kind == "bar":
        return BarMsg(
            d["symbol"], ts, Decimal(d["open"]), Decimal(d["high"]),
            Decimal(d["low"]), Decimal(d["close"]), int(d["volume"]),
            Decimal(d["vwap"]) if d.get("vwap") else None,
            d.get("trade_count"),
        )
    raise ValueError(f"unknown message type {kind!r}")


class Recorder:
    """Wraps a MessageSource and writes everything it yields to disk."""

    def __init__(self, inner, path: str | Path):
        self.inner = inner
        self.path = Path(path)
        self.count = 0

    async def stream(self, symbols: list[str]) -> AsyncIterator[Message]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            async for msg in self.inner.stream(symbols):
                fh.write(json.dumps(_encode(msg)) + "\n")
                fh.flush()
                self.count += 1
                yield msg


class ReplaySource:
    """Replays a recorded file as a MessageSource.

    speed=0 replays as fast as possible (the default, for tests).
    speed=1 replays in real time; speed=10 replays ten times faster.
    """

    def __init__(self, path: str | Path, speed: float = 0.0):
        self.path = Path(path)
        self.speed = speed

    async def stream(self, symbols: list[str]) -> AsyncIterator[Message]:
        wanted = {s.upper() for s in symbols} if symbols else None
        prev_ts: datetime | None = None

        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                msg = _decode(json.loads(line))
                if wanted is not None and msg.symbol.upper() not in wanted:
                    continue

                if self.speed > 0 and prev_ts is not None:
                    delta = (msg.ts - prev_ts).total_seconds() / self.speed
                    if delta > 0:
                        await asyncio.sleep(min(delta, 5.0))
                prev_ts = msg.ts

                yield msg

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
