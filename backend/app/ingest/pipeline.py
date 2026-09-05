"""The live ingest pipeline: bars -> indicators -> strategies -> signals.

This is the piece that was missing entirely before tonight (see the task
brief): `app/strategies/engine.py`'s `SymbolEngine`/`evaluate_strategies` and
`app/strategies/signal_service.py`'s `persist_signal` already existed from
Phase 3, but nothing called them outside `app/backtest/runner.py`. This
module is what actually drives them from real (or replayed) market data.

**Hard boundary — read this before changing anything here.** This pipeline
only ever writes `Bar`, `IngestState`, `GapEvent`, and `SignalRecord` rows.
It must never create an `Order`, never call anything in
`app.execution.order_service`, and never touch a `Position`/`Trade` row
except to *read* an already-open `Position` so `evaluate_strategies` knows
whether to call `evaluate()` or `manage()`. Auto-executing a live signal is a
separate, bigger decision the project owner hasn't made yet (see
CLAUDE.md and BUILD_SPEC §7.3 vs §7.4 — the risk engine and execution
service are a distinct stage from the strategy engine, and nothing here
crosses into them). If you're tempted to "just also place the order since
gate_passed is already checked" — don't. That would be a silent,
undiscussed scope expansion of exactly the kind CLAUDE.md tells us to flag
instead of doing quietly (mirroring the documented-scope-choice style of
app/backtest/verify.py's own docstring).

Documented scope decisions (given the size of this pass, all deliberate,
not oversights):

1. **Only the `1Min` timeframe is built from live ticks.** `orb` and
   `vwap_reversion` trade on `1Min` and so run live through this pipeline;
   `ema_cross` (`5Min`) and `rsi2` (`1Day`) simply never see a bar whose
   `timeframe` matches theirs here, so they never fire live yet. Building
   5Min/1Day bar aggregation live is future work — the task brief itself
   calls `1Min` "the base tick timeframe basically every strategy needs"
   and asks it be prioritized given the time available.
2. **Vendor-supplied `BarMsg` messages are not used to finalize bars.**
   Alpaca's live feed also pushes its own pre-aggregated 1-minute bars, but
   only `TradeMsg` ticks are aggregated via `BarBuilder` here — the same
   tested code path Phase 1's `tools/smoke_replay.py` already exercises.
   Reconciling two independent bar sources (our own trade aggregation vs.
   Alpaca's own bars) is a hardening item, not part of this pass.
   `QuoteMsg` isn't consumed at all yet — nothing here reads spread.
3. **No true hot-swap of the live subscription mid-connection.** The
   desired symbol set (open positions at priority 0, then the default
   watchlist) is recomputed at startup and on every reconnect — see
   `app/ingest/run.py` — rather than re-subscribing on Alpaca's socket
   without dropping it. `SubscriptionManager` genuinely governs the active
   set; it just doesn't yet support adding/dropping symbols on an already
   open connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.execution.positions import OpenPosition
from app.ingest.bars import BarBuilder, FinalBar, detect_gaps
from app.ingest.stream import BarMsg, Message, QuoteMsg, TradeMsg
from app.market_calendar import NY, SESSION_OPEN, start_of_trading_day
from app.models.account import PaperAccount
from app.models.market import Bar, GapEvent, IngestState
from app.models.positions import Position
from app.models.strategies import StrategyRecord
from app.strategies.base import BarContext
from app.strategies.engine import SymbolEngine, evaluate_strategies
from app.strategies.registry import STRATEGIES, create_strategy
from app.strategies.signal_service import persist_signal

log = structlog.get_logger(__name__)

# The one timeframe this pipeline builds live bars for. See module docstring,
# scope decision 1.
LIVE_BAR_TIMEFRAME = "1Min"


class HistoricalBarProvider(Protocol):
    def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[FinalBar]: ...


class AlpacaHistoricalBarProvider:
    """Default provider for both hydration and gap backfill.

    Reuses `app.backtest.data.fetch_historical_bars` (the same cached Alpaca
    REST fetch the backtest gate uses) rather than a second fetch path —
    per the task brief.
    """

    def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[FinalBar]:
        from app.backtest.data import fetch_historical_bars

        return fetch_historical_bars(symbol, timeframe, start, end)


def _ensure_utc(ts: datetime) -> datetime:
    """SQLite doesn't preserve tzinfo across a round trip; treat a naive
    value read back from the DB as UTC (everything this pipeline writes is
    always UTC). A no-op against Postgres, whose TIMESTAMPTZ columns come
    back already timezone-aware."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _to_open_position(row: Position) -> OpenPosition:
    return OpenPosition(
        symbol=row.symbol,
        side="buy",  # this build only ever opens longs — BUILD_SPEC §0.3
        qty=row.qty,
        avg_entry_price=row.avg_entry_price,
        stop_price=row.stop_price,
        opened_at=row.opened_at,
    )


class LiveIngestPipeline:
    """Owns per-symbol bar state and drives the strategy engine from it.

    Fed by `on_message` (a synchronous callback suitable for
    `app.ingest.stream.StreamRunner`'s `on_message`), which only ever does a
    cheap, non-blocking enqueue — the real async work (DB writes, strategy
    evaluation) happens in `run_consumer`, so every finalized bar is
    processed strictly in the order it was enqueued, never concurrently with
    itself. This mirrors the same callback-to-async-queue bridge
    `app.ingest.alpaca_source.AlpacaSource` already uses internally.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
        hydration_provider: HistoricalBarProvider | None = None,
        backfill_provider: HistoricalBarProvider | None = None,
    ):
        self._sessionmaker = sessionmaker
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._hydration = hydration_provider or AlpacaHistoricalBarProvider()
        self._backfill = backfill_provider or AlpacaHistoricalBarProvider()

        self._builders: dict[str, BarBuilder] = {}
        self._engines: dict[str, SymbolEngine] = {}
        self._account_cache: dict[UUID, UUID | None] = {}
        self._active_symbols: list[str] = []
        self._queue: asyncio.Queue[Message] = asyncio.Queue()

        # Test/observability hooks — never read by production logic.
        self.bars_finalized = 0
        self.signals_written = 0

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def set_active_symbols(self, symbols: list[str]) -> None:
        self._active_symbols = [s.upper() for s in symbols]

    def on_message(self, msg: Message) -> None:
        """`StreamRunner`'s `on_message` callback. Never blocks."""
        self._queue.put_nowait(msg)

    async def run_consumer(self) -> None:
        """Drains the queue forever, one message at a time, in arrival order.

        Calls `task_done()` after each message so a caller (production code
        doesn't need this; the replay-based tests do) can `await
        pipeline._queue.join()` to know every already-enqueued message has
        finished processing.
        """
        while True:
            msg = await self._queue.get()
            try:
                await self._handle_message(msg)
            finally:
                self._queue.task_done()

    async def run_finalize_ticker(self, interval_seconds: float = 1.0) -> None:
        """Wall-clock finalization for thin symbols with no next-bar tick.

        BUILD_SPEC §7.2: a bar with no further trades in the following
        minute still needs to finalize once the grace period elapses.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            now = self._clock()
            for builder in list(self._builders.values()):
                bar = builder.maybe_finalize(now)
                if bar is not None:
                    await self._on_finalized_bar(bar)

    async def hydrate(self, symbols: list[str], now: datetime, lookback_bars: int = 250) -> None:
        """Warm each symbol's `SymbolEngine` with recent real history.

        In-memory only: these bars are NOT re-persisted as `Bar` rows (they
        are already historical; live persistence starts once real bars
        arrive over the stream) — only fed to the engine so indicators like
        EMA(200) aren't cold-starting from zero on the first live tick.
        """
        for symbol in symbols:
            engine = self._engines.setdefault(symbol, SymbolEngine(symbol))

            try:
                minute_bars = self._hydration.get_bars(
                    symbol, LIVE_BAR_TIMEFRAME, now - timedelta(days=5), now
                )
            except Exception as exc:  # noqa: BLE001 - hydration is best-effort
                log.error("ingest.hydrate_minute_failed", symbol=symbol, error=str(exc))
                minute_bars = []
            for bar in minute_bars[-lookback_bars:]:
                try:
                    engine.on_finalized_bar(bar)
                except ValueError:
                    continue

            try:
                daily_bars = self._hydration.get_bars(
                    symbol, "1Day", now - timedelta(days=400), now
                )
            except Exception as exc:  # noqa: BLE001 - hydration is best-effort
                log.error("ingest.hydrate_daily_failed", symbol=symbol, error=str(exc))
                daily_bars = []
            for bar in daily_bars[-260:]:
                try:
                    engine.on_finalized_bar(bar)
                except ValueError:
                    continue

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: Message) -> None:
        if isinstance(msg, TradeMsg):
            builder = self._builders.setdefault(
                msg.symbol,
                BarBuilder(
                    msg.symbol, LIVE_BAR_TIMEFRAME,
                    grace_seconds=self.settings.bar_finalize_grace_seconds,
                ),
            )
            final = builder.on_trade(msg.ts, msg.price, msg.size)
            if final is not None:
                await self._on_finalized_bar(final)
        elif isinstance(msg, (BarMsg, QuoteMsg)):
            # Deliberately dropped — see module docstring, scope decision 2.
            return

    async def _on_finalized_bar(self, bar: FinalBar) -> None:
        async with self._sessionmaker() as db:
            await self._upsert_bar(db, bar)
            await self._update_ingest_state(db, bar)
            await db.commit()
        self.bars_finalized += 1

        engine = self._engines.setdefault(bar.symbol, SymbolEngine(bar.symbol))
        try:
            ctx = engine.on_finalized_bar(bar)
        except ValueError as exc:
            # An out-of-order/duplicate bar (e.g. hydration and the first
            # live tick overlapping, or a backfilled bar re-arriving) — log
            # and move on rather than crash the whole pipeline. See
            # SymbolEngine.on_finalized_bar's own docstring for why this
            # raises in the first place.
            log.warning(
                "ingest.bar_rejected", symbol=bar.symbol, timeframe=bar.timeframe,
                ts=bar.ts.isoformat(), error=str(exc),
            )
            return

        await self._evaluate_and_persist_signals(bar, ctx)

    async def _upsert_bar(self, db: AsyncSession, bar: FinalBar) -> None:
        existing = await db.get(Bar, (bar.symbol, bar.timeframe, bar.ts))
        if existing is None:
            db.add(Bar(
                symbol=bar.symbol, timeframe=bar.timeframe, ts=bar.ts,
                open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                volume=bar.volume, vwap=bar.vwap, trade_count=bar.trade_count,
                source=bar.source,
            ))
        else:
            existing.open = bar.open
            existing.high = bar.high
            existing.low = bar.low
            existing.close = bar.close
            existing.volume = bar.volume
            existing.vwap = bar.vwap
            existing.trade_count = bar.trade_count
            existing.source = bar.source

    async def _update_ingest_state(self, db: AsyncSession, bar: FinalBar) -> None:
        state = await db.get(IngestState, (bar.symbol, bar.timeframe))
        if state is None:
            state = IngestState(symbol=bar.symbol, timeframe=bar.timeframe)
            db.add(state)
        state.last_bar_ts = bar.ts
        state.last_message_at = self._clock()

    async def _resolve_account_id(self, db: AsyncSession, user_id: UUID) -> UUID | None:
        """Same lookup `app.deps.get_current_user_account` does for an HTTP
        request — reused here rather than a second account-resolution path."""
        if user_id in self._account_cache:
            return self._account_cache[user_id]
        account = (
            await db.execute(select(PaperAccount).where(PaperAccount.user_id == user_id))
        ).scalar_one_or_none()
        resolved = account.id if account is not None else None
        self._account_cache[user_id] = resolved
        return resolved

    async def _evaluate_and_persist_signals(self, bar: FinalBar, ctx: BarContext) -> None:
        async with self._sessionmaker() as db:
            strategy_rows = (await db.execute(select(StrategyRecord))).scalars().all()
            wrote_any = False

            for row in strategy_rows:
                strategy_cls = STRATEGIES.get(row.slug)
                if strategy_cls is None or strategy_cls.timeframe != bar.timeframe:
                    continue

                account_id = await self._resolve_account_id(db, row.user_id)
                if account_id is None:
                    log.warning("ingest.strategy_without_account", strategy_id=str(row.id))
                    continue

                position_row = (
                    await db.execute(
                        select(Position).where(
                            Position.account_id == account_id,
                            Position.symbol == bar.symbol,
                            Position.status == "open",
                        )
                    )
                ).scalar_one_or_none()
                open_position = (
                    _to_open_position(position_row) if position_row is not None else None
                )

                strategy = create_strategy(row.slug, row.params)
                signals = evaluate_strategies([strategy], ctx, open_position)

                for signal in signals:
                    # HARD BOUNDARY — see module docstring. Only the
                    # evidence record is ever written here.
                    await persist_signal(db, signal, row.id, account_id, bar.ts)
                    wrote_any = True
                    self.signals_written += 1

            if wrote_any:
                await db.commit()

    # ------------------------------------------------------------------
    # Reconnect / gap handling. BUILD_SPEC §7.1: never leave a hole.
    # ------------------------------------------------------------------

    async def handle_reconnect(self) -> None:
        """Wired as `StreamRunner`'s `on_reconnect` hook.

        Detects any hole in today's bar series for each active symbol since
        the session opened, attempts to backfill it via the injected
        `backfill_provider`, and persists a `GapEvent` either way — resolved
        if the backfill covered every expected bar, unresolved otherwise.
        BUILD_SPEC §5/§7.1: "the system must never pretend a gap did not
        happen," so the row is written regardless of whether backfill
        succeeds.
        """
        now = self._clock()
        # The regular session's open (09:30 ET), not midnight -- gaps before
        # the market opens aren't gaps. Same pattern as indicators.py's
        # opening-range window.
        day_start = start_of_trading_day(now)
        session_start = day_start.astimezone(NY).replace(
            hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute
        )

        async with self._sessionmaker() as db:
            for symbol in list(self._active_symbols):
                have_rows = (
                    await db.execute(
                        select(Bar.ts).where(
                            Bar.symbol == symbol,
                            Bar.timeframe == LIVE_BAR_TIMEFRAME,
                            Bar.ts >= session_start,
                            Bar.ts < now,
                        )
                    )
                ).scalars().all()
                # SQLite (the in-memory test DB -- see tests/conftest.py) does
                # not round-trip tzinfo the way Postgres/asyncpg does; treat a
                # naive value as UTC (everything here always is). A no-op
                # against a real Postgres/TIMESTAMPTZ column.
                have = [_ensure_utc(t) for t in have_rows]

                gaps = detect_gaps(symbol, LIVE_BAR_TIMEFRAME, have, session_start, now)
                if not gaps:
                    continue

                state = await db.get(IngestState, (symbol, LIVE_BAR_TIMEFRAME))

                for gap in gaps:
                    try:
                        backfill_bars = self._backfill.get_bars(
                            symbol, LIVE_BAR_TIMEFRAME, gap.start, gap.end
                        )
                    except Exception as exc:  # noqa: BLE001 - never crash on a failed backfill
                        log.error("ingest.backfill_failed", symbol=symbol, error=str(exc))
                        backfill_bars = []

                    filled = 0
                    for backfilled_bar in backfill_bars:
                        await self._on_finalized_bar(backfilled_bar)
                        filled += 1

                    resolved = filled >= gap.expected_bars
                    db.add(GapEvent(
                        symbol=symbol, timeframe=LIVE_BAR_TIMEFRAME,
                        gap_start=gap.start, gap_end=gap.end,
                        expected_bars=gap.expected_bars, filled_bars=filled,
                        resolved=resolved,
                        detail=f"detected on reconnect at {now.isoformat()}",
                        resolved_at=now if resolved else None,
                    ))
                    if state is not None:
                        state.gap_count += 1

                    log.warning(
                        "ingest.gap_detected", symbol=symbol,
                        expected_bars=gap.expected_bars, filled_bars=filled, resolved=resolved,
                    )

            await db.commit()


__all__ = [
    "LiveIngestPipeline",
    "HistoricalBarProvider",
    "AlpacaHistoricalBarProvider",
    "LIVE_BAR_TIMEFRAME",
]
