"""The live ingest process. BUILD_SPEC §4, §7.1: single instance, owns the
Alpaca data socket.

    python -m app.ingest.run

Wires together the pieces that already existed as tested, pure library code
before tonight (`AlpacaSource`, `StreamRunner`, `BarBuilder`, `SymbolEngine`,
`SubscriptionManager`) with the new `app.ingest.pipeline.LiveIngestPipeline`,
which is what actually persists bars and writes `SignalRecord` rows. See
that module's docstring for the hard boundary this process respects: it
NEVER places an order, regardless of any strategy's `enabled`/`gate_passed`
state.

Untested by this task except via the replay harness (see
tests/test_ingest_pipeline.py) — the real `AlpacaSource` websocket path
itself only proves out at a real market open, which this session cannot
exercise.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.ingest.alpaca_source import AlpacaSource
from app.ingest.pipeline import LiveIngestPipeline
from app.ingest.stream import StreamRunner
from app.ingest.subscriptions import SubscriptionManager
from app.models.positions import Position

log = structlog.get_logger(__name__)


async def _desired_symbols(sessionmaker, settings: Settings) -> dict[str, int]:
    """Symbols with an open Position (any account) get priority 0; the rest
    of the default watchlist fills in behind them, in listed order."""
    async with sessionmaker() as db:
        open_symbols = (
            await db.execute(select(Position.symbol).where(Position.status == "open").distinct())
        ).scalars().all()

    desired: dict[str, int] = {s.upper(): 0 for s in open_symbols}
    for priority, symbol in enumerate(settings.default_watchlist, start=1):
        desired.setdefault(symbol.upper(), priority)
    return desired


async def main() -> int:
    settings = get_settings()  # raises if the paper-only rails are violated
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise SystemExit(
            "ALPACA_API_KEY/ALPACA_API_SECRET are required to run live ingest. "
            "Create a free paper account at alpaca.markets (email only) and set them."
        )

    sessionmaker = get_sessionmaker()
    sub_manager = SubscriptionManager(max_symbols=settings.max_stream_symbols)

    desired = await _desired_symbols(sessionmaker, settings)
    diff = sub_manager.set_desired(desired)
    symbols = sorted(sub_manager.active)
    log.info(
        "ingest.subscriptions", symbols=symbols,
        subscribe=diff.subscribe, unsubscribe=diff.unsubscribe,
    )
    if not symbols:
        raise SystemExit("no symbols to subscribe to (empty watchlist and no open positions)")

    pipeline = LiveIngestPipeline(sessionmaker, settings)
    pipeline.set_active_symbols(symbols)

    now = datetime.now(UTC)
    log.info("ingest.hydrating", symbols=symbols)
    await pipeline.hydrate(symbols, now)
    log.info("ingest.hydrated")

    async def on_reconnect() -> None:
        # Gap backfill first (BUILD_SPEC §7.1: never leave a hole), then
        # recompute the desired symbol set for the connection about to be
        # (re)established — see module docstring, scope decision 3.
        await pipeline.handle_reconnect()
        new_desired = await _desired_symbols(sessionmaker, settings)
        new_diff = sub_manager.set_desired(new_desired)
        if not new_diff.is_empty:
            log.info(
                "ingest.resubscribing", subscribe=new_diff.subscribe,
                unsubscribe=new_diff.unsubscribe,
            )
        pipeline.set_active_symbols(sorted(sub_manager.active))

    source = AlpacaSource(
        settings.alpaca_api_key, settings.alpaca_api_secret, settings.alpaca_data_feed
    )
    runner = StreamRunner(
        source=source,
        on_message=pipeline.on_message,
        heartbeat_timeout=settings.stream_heartbeat_timeout_seconds,
        on_reconnect=on_reconnect,
    )

    consumer_task = asyncio.create_task(pipeline.run_consumer())
    ticker_task = asyncio.create_task(pipeline.run_finalize_ticker())

    try:
        await runner.run(sorted(sub_manager.active))
    finally:
        consumer_task.cancel()
        ticker_task.cancel()
        for t in (consumer_task, ticker_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
