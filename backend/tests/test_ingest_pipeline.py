"""Integration tests for the live ingest pipeline (app/ingest/pipeline.py).

Runs the REAL `StreamRunner` + `BarBuilder` + `SymbolEngine` +
`evaluate_strategies` + `persist_signal` pipeline against
`app.ingest.replay.ReplaySource` instead of the live `AlpacaSource` — this is
exactly the substitution BUILD_SPEC's replay harness (§18, built in Phase 1)
exists for, and it's what makes this feature testable outside market hours.

Nothing here ever hits the network: `AlpacaSource`/`fetch_historical_bars`
(both of which the pipeline would otherwise use for the live socket and for
hydration/backfill respectively) are never constructed -- fakes stand in for
both. Only synthetic single-trade-per-minute bars are used (open == high ==
low == close by construction), so exact indicator values are fully
predictable without needing pandas-ta internals.

Untested here, and honestly flagged as such in the task summary: the real
`AlpacaSource` websocket adapter itself. That only proves out against a live
market, which this session cannot exercise.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.ingest.bars import FinalBar
from app.ingest.pipeline import LiveIngestPipeline
from app.ingest.replay import ReplaySource
from app.ingest.stream import StreamRunner
from app.models import Base
from app.models.account import PaperAccount, User
from app.models.market import Bar, GapEvent
from app.models.orders import Order
from app.models.positions import Position
from app.models.signals import SignalRecord
from app.models.strategies import StrategyRecord

SYMBOL = "XLF"
SESSION_START = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)  # 09:30 ET


def _utc(ts: datetime) -> datetime:
    """SQLite (the in-memory test DB) doesn't round-trip tzinfo the way
    Postgres/asyncpg does -- a value read back after a fresh query comes
    back naive. Normalize before comparing; everything here is always UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _write_recording(path: Path, ticks: list[tuple[int, float, int]], symbol: str = SYMBOL) -> None:
    """One trade per (minute offset, price, size) entry -- matches
    app.ingest.replay's on-disk ndjson schema directly."""
    with path.open("w", encoding="utf-8") as fh:
        for minute, price, size in ticks:
            ts = SESSION_START + timedelta(minutes=minute)
            fh.write(json.dumps({
                "t": "trade", "symbol": symbol, "ts": ts.isoformat(),
                "price": str(price), "size": size,
            }) + "\n")


class FakeHydrationProvider:
    """Daily bars, flat at 80.00 -- vwap_reversion's trend filter needs
    close > daily EMA(200); 220 flat bars converge EMA(200) to ~80.00, well
    below every intraday price used in these tests.

    Optionally also supplies `warmup_1min_bars` bars of 1Min history, ending
    just before SESSION_START, at `warmup_1min_price` -- used by
    TestGenuineExitWithMixedConditions so the very first LIVE bar isn't the
    only data point session VWAP has ever seen (a lone bar's VWAP always
    trivially equals its own close, which would make vwap_touch fire on
    bar zero regardless of price action)."""

    def __init__(self, warmup_1min_bars: int = 0, warmup_1min_price: Decimal = Decimal("105.00")):
        self.warmup_1min_bars = warmup_1min_bars
        self.warmup_1min_price = warmup_1min_price

    def get_bars(self, symbol: str, timeframe: str, start, end) -> list[FinalBar]:
        if timeframe == "1Day":
            base = SESSION_START - timedelta(days=250)
            price = Decimal("80.00")
            return [
                FinalBar(
                    symbol=symbol, timeframe="1Day", ts=base + timedelta(days=i),
                    open=price, high=price, low=price, close=price,
                    volume=1_000_000, vwap=None, trade_count=1000,
                )
                for i in range(220)
            ]
        if timeframe == "1Min" and self.warmup_1min_bars:
            price = self.warmup_1min_price
            return [
                FinalBar(
                    symbol=symbol, timeframe="1Min",
                    ts=SESSION_START - timedelta(minutes=self.warmup_1min_bars - i),
                    open=price, high=price, low=price, close=price,
                    volume=1000, vwap=None, trade_count=1,
                )
                for i in range(self.warmup_1min_bars)
            ]
        return []


class EmptyBackfillProvider:
    """No historical data available -- the gap stays unresolved. Used for
    the reconnect/gap test, which only cares that the gap gets *recorded*,
    not that it can actually be filled."""

    def get_bars(self, symbol: str, timeframe: str, start, end) -> list[FinalBar]:
        return []


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()


async def _seed_account_and_strategy(
    sessionmaker, slug: str, enabled: bool = True, gate_passed: bool = True,
) -> tuple[PaperAccount, StrategyRecord]:
    async with sessionmaker() as session:
        user = User(
            id=uuid4(), email=f"{uuid4()}@example.com", password_hash="x", role="requester",
        )
        account = PaperAccount(
            id=uuid4(), user_id=user.id, name="test",
            starting_cash=Decimal("100000"), cash=Decimal("100000"), equity=Decimal("100000"),
        )
        strategy = StrategyRecord(
            id=uuid4(), user_id=user.id, slug=slug, name=f"My {slug}", params={},
            enabled=enabled, gate_passed=gate_passed,
        )
        session.add_all([user, account, strategy])
        await session.commit()
        return account, strategy


async def _run_replay(pipeline: LiveIngestPipeline, path: Path, symbol: str = SYMBOL) -> None:
    consumer = asyncio.ensure_future(pipeline.run_consumer())
    runner = StreamRunner(source=ReplaySource(path, speed=0.0), on_message=pipeline.on_message)
    await runner.run([symbol], max_reconnects=0)
    await pipeline._queue.join()  # every enqueued message has finished processing
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass


class TestGenuineEntryFiresEndToEnd:
    """vwap_reversion's evaluate() path: no open position, so
    evaluate_strategies calls evaluate(), never manage()."""

    @pytest.mark.asyncio
    async def test_entry_signal_written_with_full_evidence(self, db, tmp_path):
        account, strategy = await _seed_account_and_strategy(db, "vwap_reversion")

        # 20 flat minutes at 100.00, then one bar dipping to 95.00 (more than
        # 2 std below session VWAP once diluted into the average -- see the
        # task's own worked arithmetic in the module docstring), then one
        # more tick to finalize that dip bar.
        ticks = [(m, 100.00, 1000) for m in range(20)] + [(20, 95.00, 1000), (21, 100.00, 1000)]
        path = tmp_path / "vwap_dip.ndjson"
        _write_recording(path, ticks)

        pipeline = LiveIngestPipeline(
            db, Settings(), clock=lambda: SESSION_START,
            hydration_provider=FakeHydrationProvider(),
            backfill_provider=EmptyBackfillProvider(),
        )
        await pipeline.hydrate([SYMBOL], SESSION_START)

        await _run_replay(pipeline, path)

        async with db() as session:
            signals = (
                await session.execute(select(SignalRecord).order_by(SignalRecord.ts))
            ).scalars().all()
            orders = (await session.execute(select(Order))).scalars().all()
            bars = (
                await session.execute(
                    select(Bar).where(Bar.symbol == SYMBOL).order_by(Bar.ts)
                )
            ).scalars().all()

        # Bars finalize strictly in order.
        assert len(bars) == 21  # minutes 0..20 finalized; minute 21 still working
        assert all(_utc(bars[i].ts) < _utc(bars[i + 1].ts) for i in range(len(bars) - 1))

        assert len(signals) == 1
        signal = signals[0]
        assert signal.account_id == account.id
        assert signal.strategy_id == strategy.id
        assert signal.symbol == SYMBOL
        assert signal.side == "buy"
        assert signal.intent == "entry"
        assert signal.acted_on is False

        # The signal's ts is exactly the triggering (dip) bar's ts -- never
        # later, never a different bar. CLAUDE.md rule 4: the dip bar is
        # also the LAST finalized bar at the moment the signal fired, i.e.
        # no bar timestamped after it existed yet.
        dip_bar_ts = SESSION_START + timedelta(minutes=20)
        assert _utc(signal.ts) == dip_bar_ts
        assert _utc(bars[-1].ts) == dip_bar_ts

        # Complete, non-empty condition list (both happen to be True here --
        # an ENTRY signal only ever gets returned once every condition has
        # passed; the "including a failed condition" half of the contract is
        # covered by TestGenuineExitWithMixedConditions below and by
        # tests/test_signal_service.py's round-trip test).
        assert len(signal.conditions) == 2
        names = {c["name"] for c in signal.conditions}
        assert names == {"vwap_oversold", "trend_filter_up"}
        assert all(c["passed"] for c in signal.conditions)

        # HARD BOUNDARY: this pipeline never places an order, under any
        # strategy enabled/gate_passed combination -- the seeded strategy
        # here is enabled=True, gate_passed=True (the "best case" for
        # auto-execution), and still zero Order rows exist.
        assert orders == []


class TestGenuineExitWithMixedConditions:
    """vwap_reversion's manage() path: an open Position (seeded directly,
    the way a real position would exist from a completely separate manual
    order -- this pipeline never opens one itself) makes evaluate_strategies
    call manage() instead. This is the strategy's ATR-stop exit, which
    reports BOTH conditions regardless of which one fired -- vwap_touch
    False, atr_stop_hit True -- proving the full evidence list, including a
    failed condition, survives all the way from the strategy through the
    live pipeline into the signals table."""

    @pytest.mark.asyncio
    async def test_exit_signal_includes_the_failed_condition(self, db, tmp_path):
        account, strategy = await _seed_account_and_strategy(db, "vwap_reversion")

        async with db() as session:
            order = Order(
                account_id=account.id, symbol=SYMBOL, side="buy", qty=Decimal("10"),
                order_type="market", status="filled", source="manual",
            )
            session.add(order)
            await session.flush()
            session.add(Position(
                account_id=account.id, entry_order_id=order.id, symbol=SYMBOL,
                qty=Decimal("10"), avg_entry_price=Decimal("97.50"),
                stop_price=Decimal("96.00"), opened_at=SESSION_START, status="open",
            ))
            await session.commit()

        # A monotonically DECREASING price series, continuing straight out of
        # one hydrated warm-up bar at 105.00: 104, 103, ..., 96 (minutes 0-8),
        # then one more tick to finalize the last one. For any strictly
        # decreasing series, the running VWAP at step k is the average of
        # every prior (higher) value plus the current one, so it always sits
        # ABOVE the current close -- vwap_touch (close >= vwap) stays False
        # at every single bar, including the last one, where close(96.00)
        # also happens to equal the position's 96.00 stop -- atr_stop_hit
        # fires True there and only there.
        ticks = [(m, 104.00 - m, 1000) for m in range(9)] + [(9, 90.00, 1000)]
        path = tmp_path / "vwap_stop.ndjson"
        _write_recording(path, ticks)

        pipeline = LiveIngestPipeline(
            db, Settings(), clock=lambda: SESSION_START,
            hydration_provider=FakeHydrationProvider(
                warmup_1min_bars=1, warmup_1min_price=Decimal("105.00"),
            ),
            backfill_provider=EmptyBackfillProvider(),
        )
        await pipeline.hydrate([SYMBOL], SESSION_START)

        await _run_replay(pipeline, path)

        async with db() as session:
            signals = (await session.execute(select(SignalRecord))).scalars().all()
            orders = (await session.execute(select(Order))).scalars().all()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.intent == "exit"
        assert signal.side == "sell"
        assert signal.rule_id == "vwap_reversion.stop_hit"

        by_name = {c["name"]: c for c in signal.conditions}
        assert len(by_name) == 2
        assert by_name["atr_stop_hit"]["passed"] is True
        assert by_name["vwap_touch"]["passed"] is False  # the failed condition, preserved

        # Still exactly the ONE pre-seeded Order (from this test's own setup,
        # not from the pipeline) -- the pipeline created zero more.
        assert len(orders) == 1


class TestGapDetectionOnReconnect:
    """Reuses tools/smoke_replay.py's own pattern: a deliberate feed outage,
    baked into the recording, that detect_gaps must catch. BUILD_SPEC §5/
    §7.1: 'the system must never pretend a gap did not happen.'"""

    @pytest.mark.asyncio
    async def test_gap_event_recorded_for_the_deliberate_outage(self, db, tmp_path):
        minutes = 30
        gap_at, gap_len = 12, 3

        ticks = [
            (m, 50.00 + m * 0.01, 500)
            for m in range(minutes + 1)  # +1: one trailing tick finalizes minute 29's bar
            if not (gap_at <= m < gap_at + gap_len)
        ]
        path = tmp_path / "gap.ndjson"
        _write_recording(path, ticks)

        pipeline = LiveIngestPipeline(
            db, Settings(), clock=lambda: SESSION_START + timedelta(minutes=minutes),
            hydration_provider=FakeHydrationProvider(),
            backfill_provider=EmptyBackfillProvider(),
        )
        pipeline.set_active_symbols([SYMBOL])

        await _run_replay(pipeline, path)

        # Nothing detected yet -- gap detection only runs on reconnect.
        async with db() as session:
            assert (await session.execute(select(GapEvent))).scalars().all() == []

        await pipeline.handle_reconnect()

        async with db() as session:
            gaps = (
                await session.execute(select(GapEvent).where(GapEvent.symbol == SYMBOL))
            ).scalars().all()

        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.expected_bars == gap_len
        assert gap.filled_bars == 0  # EmptyBackfillProvider supplies nothing
        assert gap.resolved is False
        assert _utc(gap.gap_start) == SESSION_START + timedelta(minutes=gap_at)
