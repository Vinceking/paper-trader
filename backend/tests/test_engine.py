"""Strategy engine tests. BUILD_SPEC §7.2, §7.3, Phase 3 acceptance criteria:

✅ A test proves no indicator reads a bar with ts > signal_ts (no lookahead).
✅ Signals fire only on finalized bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.execution.positions import OpenPosition
from app.ingest.bars import BarBuilder, FinalBar
from app.strategies.base import BarContext, Signal, Strategy
from app.strategies.engine import SymbolEngine, evaluate_strategies

START = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)


def make_bar(minutes: int, price: float, symbol="XLF", timeframe="1Min", vol=1000) -> FinalBar:
    return FinalBar(
        symbol=symbol, timeframe=timeframe, ts=START + timedelta(minutes=minutes),
        open=Decimal(str(price)), high=Decimal(str(price + 0.1)),
        low=Decimal(str(price - 0.1)), close=Decimal(str(price)),
        volume=vol, vwap=None, trade_count=1,
    )


class TestSymbolEngineHistory:
    def test_builds_rolling_history_in_order(self):
        engine = SymbolEngine("XLF")
        for i in range(5):
            engine.on_finalized_bar(make_bar(i, 100 + i))
        history = engine.history_for("1Min")
        assert [b.close for b in history] == [Decimal(str(100 + i)) for i in range(5)]

    def test_bounds_history_to_max(self):
        engine = SymbolEngine("XLF", max_history=3)
        for i in range(5):
            engine.on_finalized_bar(make_bar(i, 100 + i))
        history = engine.history_for("1Min")
        assert len(history) == 3
        assert [b.close for b in history] == [Decimal("102"), Decimal("103"), Decimal("104")]

    def test_rejects_out_of_order_bar(self):
        engine = SymbolEngine("XLF")
        engine.on_finalized_bar(make_bar(5, 100))
        with pytest.raises(ValueError, match="out-of-order"):
            engine.on_finalized_bar(make_bar(3, 101))

    def test_rejects_duplicate_bar_timestamp(self):
        engine = SymbolEngine("XLF")
        engine.on_finalized_bar(make_bar(5, 100))
        with pytest.raises(ValueError, match="out-of-order"):
            engine.on_finalized_bar(make_bar(5, 101))

    def test_rejects_bar_for_wrong_symbol(self):
        engine = SymbolEngine("XLF")
        with pytest.raises(ValueError, match="XLE"):
            engine.on_finalized_bar(make_bar(0, 100, symbol="XLE"))

    def test_separate_timeframes_tracked_independently(self):
        engine = SymbolEngine("XLF")
        engine.on_finalized_bar(make_bar(0, 100, timeframe="1Min"))
        engine.on_finalized_bar(make_bar(0, 100, timeframe="5Min"))
        assert len(engine.history_for("1Min")) == 1
        assert len(engine.history_for("5Min")) == 1


class TestDailyContext:
    def test_daily_context_none_without_daily_bars(self):
        engine = SymbolEngine("XLF")
        ctx = engine.on_finalized_bar(make_bar(0, 100, timeframe="1Min"))
        assert ctx.daily_history is None
        assert ctx.daily_indicators is None

    def test_daily_context_populated_once_daily_bars_exist(self):
        engine = SymbolEngine("XLF")
        engine.on_finalized_bar(make_bar(0, 100, timeframe="1Day"))
        ctx = engine.on_finalized_bar(make_bar(1, 101, timeframe="1Min"))
        assert ctx.daily_history is not None
        assert len(ctx.daily_history) == 1
        assert ctx.daily_indicators is not None

    def test_daily_bar_itself_gets_no_separate_daily_context(self):
        """A 1Day bar's own context doesn't duplicate itself as 'daily'."""
        engine = SymbolEngine("XLF")
        ctx = engine.on_finalized_bar(make_bar(0, 100, timeframe="1Day"))
        assert ctx.daily_history is None


class _StubStrategy(Strategy):
    slug = "stub"
    timeframe = "1Min"
    default_params: dict = {}

    def __init__(self, entry_signal=None, exit_signal=None):
        super().__init__()
        self._entry_signal = entry_signal
        self._exit_signal = exit_signal
        self.evaluate_calls = 0
        self.manage_calls = 0

    def evaluate(self, ctx: BarContext) -> Signal | None:
        self.evaluate_calls += 1
        return self._entry_signal

    def manage(self, ctx: BarContext, position: OpenPosition) -> Signal | None:
        self.manage_calls += 1
        return self._exit_signal


def dummy_signal(rule_id="stub.rule") -> Signal:
    return Signal(
        side="buy", intent="entry", symbol="XLF", rule_id=rule_id, rule_text="stub",
        features={}, conditions=[], stop_price=Decimal("99"),
    )


def dummy_position() -> OpenPosition:
    return OpenPosition(
        symbol="XLF", side="buy", qty=Decimal("10"), avg_entry_price=Decimal("100"),
        stop_price=Decimal("99"), opened_at=START,
    )


class TestEvaluateStrategies:
    def test_calls_evaluate_when_no_open_position(self):
        engine = SymbolEngine("XLF")
        ctx = engine.on_finalized_bar(make_bar(0, 100))
        strat = _StubStrategy(entry_signal=dummy_signal())

        signals = evaluate_strategies([strat], ctx, open_position=None)

        assert strat.evaluate_calls == 1
        assert strat.manage_calls == 0
        assert signals == [strat._entry_signal]

    def test_calls_manage_when_position_open(self):
        engine = SymbolEngine("XLF")
        ctx = engine.on_finalized_bar(make_bar(0, 100))
        strat = _StubStrategy(exit_signal=dummy_signal("stub.exit"))

        signals = evaluate_strategies([strat], ctx, open_position=dummy_position())

        assert strat.manage_calls == 1
        assert strat.evaluate_calls == 0
        assert signals == [strat._exit_signal]

    def test_skips_strategy_on_a_different_timeframe(self):
        class FiveMinStrategy(_StubStrategy):
            timeframe = "5Min"

        engine = SymbolEngine("XLF")
        ctx = engine.on_finalized_bar(make_bar(0, 100, timeframe="1Min"))
        strat = FiveMinStrategy(entry_signal=dummy_signal())

        signals = evaluate_strategies([strat], ctx, open_position=None)

        assert strat.evaluate_calls == 0
        assert signals == []

    def test_no_signal_returns_empty_list(self):
        engine = SymbolEngine("XLF")
        ctx = engine.on_finalized_bar(make_bar(0, 100))
        strat = _StubStrategy()  # both signals None

        assert evaluate_strategies([strat], ctx, open_position=None) == []


# ---------------------------------------------------------------------------
# The no-lookahead property test. BUILD_SPEC §16 Phase 3 acceptance criteria.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BarSpec:
    price_delta: float
    volume: int


@st.composite
def _bar_sequence(draw):
    n = draw(st.integers(min_value=5, max_value=40))
    specs = draw(
        st.lists(
            st.builds(
                _BarSpec,
                price_delta=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
                volume=st.integers(min_value=1, max_value=5000),
            ),
            min_size=n, max_size=n,
        )
    )
    bars = []
    price = 100.0
    for i, spec in enumerate(specs):
        price = max(1.0, price + spec.price_delta)
        bars.append(make_bar(i, round(price, 4), vol=spec.volume))
    return bars


class TestNoLookahead:
    @given(bars=_bar_sequence())
    # deadline=None: each example recomputes the full indicator pipeline
    # (several pandas-ta calls) once per bar for two separate engine runs —
    # legitimately slower than hypothesis's default 200ms budget, not a bug.
    @settings(max_examples=50, deadline=None)
    def test_finalizing_later_bars_never_changes_an_earlier_context(self, bars):
        # World A: run the full sequence once, capturing the context produced
        # at every step.
        full_run_engine = SymbolEngine("XLF")
        contexts_from_full_run = [full_run_engine.on_finalized_bar(b) for b in bars]

        # World B: for an arbitrary earlier cut point, run ONLY the prefix on
        # a fresh engine. If any indicator secretly depended on bars that
        # hadn't been finalized yet, this would differ from World A's result
        # at that same step.
        cut = len(bars) // 2
        prefix_engine = SymbolEngine("XLF")
        prefix_ctx = None
        for b in bars[: cut + 1]:
            prefix_ctx = prefix_engine.on_finalized_bar(b)

        assert prefix_ctx == contexts_from_full_run[cut]
        # And explicitly: nothing in the history handed to indicators at the
        # cut point has a timestamp after the bar being finalized.
        assert all(hb.ts <= bars[cut].ts for hb in prefix_ctx.history)


# ---------------------------------------------------------------------------
# "Signals fire only on finalized bars" — integration with BarBuilder.
# ---------------------------------------------------------------------------


class TestSignalsOnlyOnFinalizedBars:
    def test_mid_bar_trades_never_trigger_evaluation(self):
        """A strategy is only ever invoked when BarBuilder actually hands
        back a FinalBar — never for the ticks building up a forming bar."""
        builder = BarBuilder("XLF")
        engine = SymbolEngine("XLF")
        strat = _StubStrategy(entry_signal=dummy_signal())

        # Three trades land within the same forming minute bar. None of them
        # produce a FinalBar, so the engine/strategy must never see them.
        for sec, price in [(1, 100.0), (20, 100.1), (45, 99.9)]:
            final = builder.on_trade(START + timedelta(seconds=sec), Decimal(str(price)), 10)
            assert final is None
            assert strat.evaluate_calls == 0  # nothing finalized yet -> nothing evaluated

        # The next minute's first tick finalizes the bar we were building.
        final = builder.on_trade(START + timedelta(minutes=1, seconds=1), Decimal("100.2"), 10)
        assert final is not None

        ctx = engine.on_finalized_bar(final)
        evaluate_strategies([strat], ctx, open_position=None)
        assert strat.evaluate_calls == 1
