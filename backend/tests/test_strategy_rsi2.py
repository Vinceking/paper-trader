"""RSI(2) Mean Reversion strategy tests. BUILD_SPEC §8.3 #4, CLAUDE.md rules 2/4/6.

Builds `BarContext`/`IndicatorSnapshot` directly rather than going through
`SymbolEngine` — the indicator pipeline itself is covered by
test_indicators.py; these tests are about the strategy's own decision logic
(condition computation, entry/exit signals, the ATR stop, the trading-day
time stop).
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.execution.positions import OpenPosition
from app.ingest.bars import FinalBar
from app.strategies.base import BarContext
from app.strategies.indicators import IndicatorSnapshot
from app.strategies.rsi2 import Rsi2MeanReversion

START = datetime(2026, 3, 2, 13, 30, tzinfo=UTC)  # a Monday, 09:30 ET


def make_bar(
    days: int,
    close: float,
    high: float | None = None,
    low: float | None = None,
    symbol: str = "AAPL",
    vol: int = 1_000_000,
    ts: datetime | None = None,
) -> FinalBar:
    high = close if high is None else high
    low = close if low is None else low
    bar_ts = ts if ts is not None else START + timedelta(days=days)
    return FinalBar(
        symbol=symbol, timeframe="1Day", ts=bar_ts,
        open=Decimal(str(close)), high=Decimal(str(high)), low=Decimal(str(low)),
        close=Decimal(str(close)), volume=vol, vwap=None, trade_count=1,
    )


_INDICATOR_FIELDS = {f.name for f in fields(IndicatorSnapshot)}


def make_indicators(**overrides) -> IndicatorSnapshot:
    defaults = dict.fromkeys(_INDICATOR_FIELDS, None)
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


def make_ctx(
    bar: FinalBar,
    indicators: IndicatorSnapshot,
    history: list[FinalBar] | None = None,
    symbol: str = "AAPL",
) -> BarContext:
    bar_history = history if history is not None else [bar]
    return BarContext(symbol=symbol, bar=bar, history=bar_history, indicators=indicators)


def make_position(
    entry: float = 100.0,
    stop: float = 97.0,
    opened_at: datetime | None = None,
) -> OpenPosition:
    return OpenPosition(
        symbol="AAPL", side="buy", qty=Decimal("10"),
        avg_entry_price=Decimal(str(entry)), stop_price=Decimal(str(stop)),
        opened_at=opened_at if opened_at is not None else START,
    )


STRAT = Rsi2MeanReversion()


class TestEntryConditions:
    def test_entry_fires_when_oversold_and_above_sma200(self):
        # rsi2 = 5 < 10 (passes); close 105 > sma200 100 (passes)
        ind = make_indicators(rsi_2=5.0, sma_200=100.0, atr_14=2.0)
        bar = make_bar(0, close=105.0)
        ctx = make_ctx(bar, ind)

        signal = STRAT.evaluate(ctx)

        assert signal is not None
        assert signal.rule_id == "rsi2.oversold_long"
        assert signal.side == "buy"
        assert signal.intent == "entry"
        # stop = entry - 1.5 * ATR = 105 - 1.5*2 = 102
        assert signal.stop_price == Decimal("102.0")

        assert len(signal.conditions) == 2
        by_name = {c.name: c for c in signal.conditions}
        assert by_name["rsi2_below_oversold"].passed is True
        assert by_name["rsi2_below_oversold"].actual == 5.0
        assert by_name["rsi2_below_oversold"].threshold == 10.0
        assert by_name["close_above_sma200"].passed is True
        assert by_name["close_above_sma200"].actual == 105.0
        assert by_name["close_above_sma200"].threshold == 100.0

    def test_no_entry_when_below_sma200_even_if_oversold(self):
        # rsi2 = 5 < 10 (passes); close 95 <= sma200 100 (fails)
        ind = make_indicators(rsi_2=5.0, sma_200=100.0, atr_14=2.0)
        bar = make_bar(0, close=95.0)
        ctx = make_ctx(bar, ind)

        conditions = STRAT._conditions(ctx)
        by_name = {c.name: c for c in conditions}
        assert by_name["rsi2_below_oversold"].passed is True
        assert by_name["rsi2_below_oversold"].actual == 5.0
        assert by_name["close_above_sma200"].passed is False
        assert by_name["close_above_sma200"].actual == 95.0

        assert STRAT.evaluate(ctx) is None

    def test_no_entry_when_not_oversold_even_if_above_sma200(self):
        # rsi2 = 40 >= 10 (fails); close 105 > sma200 100 (passes)
        ind = make_indicators(rsi_2=40.0, sma_200=100.0, atr_14=2.0)
        bar = make_bar(0, close=105.0)
        ctx = make_ctx(bar, ind)

        conditions = STRAT._conditions(ctx)
        by_name = {c.name: c for c in conditions}
        assert by_name["rsi2_below_oversold"].passed is False
        assert by_name["close_above_sma200"].passed is True

        assert STRAT.evaluate(ctx) is None

    def test_no_entry_when_rsi2_not_yet_available(self):
        ind = make_indicators(rsi_2=None, sma_200=100.0, atr_14=2.0)
        bar = make_bar(0, close=105.0)
        ctx = make_ctx(bar, ind)

        assert STRAT._conditions(ctx) is None
        assert STRAT.evaluate(ctx) is None

    def test_no_entry_when_sma200_not_yet_available(self):
        ind = make_indicators(rsi_2=5.0, sma_200=None, atr_14=2.0)
        bar = make_bar(0, close=105.0)
        ctx = make_ctx(bar, ind)

        assert STRAT._conditions(ctx) is None
        assert STRAT.evaluate(ctx) is None

    def test_no_exception_when_both_indicators_missing(self):
        ind = make_indicators()  # everything None (warm-up)
        bar = make_bar(0, close=105.0)
        ctx = make_ctx(bar, ind)

        assert STRAT._conditions(ctx) is None
        assert STRAT.evaluate(ctx) is None

    def test_entry_signal_stop_price_is_never_none(self):
        """CLAUDE.md rule 6: an entry Signal with stop_price=None gets
        rejected downstream by the risk engine — this strategy must never
        produce one."""
        scenarios = [
            (3.0, 100.0, 1.0, 105.0),
            (0.5, 50.0, 2.0, 55.0),
            (9.9, 200.0, 0.5, 200.1),
        ]
        for rsi2, sma200, atr, close in scenarios:
            ind = make_indicators(rsi_2=rsi2, sma_200=sma200, atr_14=atr)
            bar = make_bar(0, close=close)
            ctx = make_ctx(bar, ind)
            signal = STRAT.evaluate(ctx)
            assert signal is not None
            assert signal.stop_price is not None


class TestManage:
    def _ctx_for(
        self, bar: FinalBar, history: list[FinalBar] | None = None, **ind_overrides
    ) -> BarContext:
        ind = make_indicators(**ind_overrides)
        return make_ctx(bar, ind, history=history)

    def test_overbought_exit_fires(self):
        position = make_position(entry=100.0, stop=97.0, opened_at=START)
        bar = make_bar(1, close=103.0, ts=START + timedelta(days=1))
        ctx = self._ctx_for(bar, history=[bar], rsi_2=75.0, sma_5=101.0)

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "rsi2.overbought_exit"
        assert signal.intent == "exit"
        assert signal.side == "sell"

    def test_above_sma5_exit_fires_independently_of_rsi(self):
        # RSI(2) is nowhere near overbought, but close is above SMA(5).
        position = make_position(entry=100.0, stop=97.0, opened_at=START)
        bar = make_bar(1, close=103.0, ts=START + timedelta(days=1))
        ctx = self._ctx_for(bar, history=[bar], rsi_2=30.0, sma_5=101.0)

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "rsi2.above_sma5_exit"
        assert signal.intent == "exit"

    def test_no_exit_when_neither_condition_holds_and_time_stop_not_reached(self):
        position = make_position(entry=100.0, stop=97.0, opened_at=START)
        bar = make_bar(1, close=99.0, ts=START + timedelta(days=1))
        ctx = self._ctx_for(bar, history=[bar], rsi_2=30.0, sma_5=101.0)

        assert STRAT.manage(ctx, position) is None

    def test_time_stop_fires_once_five_bars_elapsed(self):
        """5 daily bars strictly after opened_at -> forced exit even though
        neither RSI(2) nor SMA(5) triggers."""
        position = make_position(entry=100.0, stop=97.0, opened_at=START)
        history = [make_bar(i, close=99.0, ts=START + timedelta(days=i)) for i in range(1, 6)]
        current = history[-1]
        ctx = self._ctx_for(current, history=history, rsi_2=30.0, sma_5=101.0)

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "rsi2.time_stop"
        assert signal.intent == "exit"

    def test_time_stop_does_not_fire_before_five_bars_elapsed(self):
        position = make_position(entry=100.0, stop=97.0, opened_at=START)
        history = [make_bar(i, close=99.0, ts=START + timedelta(days=i)) for i in range(1, 5)]
        current = history[-1]
        ctx = self._ctx_for(current, history=history, rsi_2=30.0, sma_5=101.0)

        assert STRAT.manage(ctx, position) is None

    def test_entry_bar_itself_is_not_counted_toward_time_stop(self):
        """A history that also contains the entry bar (ts == opened_at)
        must not count it — only bars strictly after entry."""
        position = make_position(entry=100.0, stop=97.0, opened_at=START)
        entry_bar = make_bar(0, close=100.0, ts=START)
        later_bars = [make_bar(i, close=99.0, ts=START + timedelta(days=i)) for i in range(1, 5)]
        history = [entry_bar, *later_bars]
        current = later_bars[-1]
        ctx = self._ctx_for(current, history=history, rsi_2=30.0, sma_5=101.0)

        # Only 4 bars strictly after opened_at -> time stop must not fire yet.
        assert STRAT.manage(ctx, position) is None

    def test_overbought_exit_reported_condition_is_correct(self):
        position = make_position(entry=100.0, stop=97.0, opened_at=START)
        bar = make_bar(1, close=103.0, ts=START + timedelta(days=1))
        ctx = self._ctx_for(bar, history=[bar], rsi_2=82.0, sma_5=101.0)

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert len(signal.conditions) == 1
        condition = signal.conditions[0]
        assert condition.passed is True
        assert condition.actual == 82.0
        assert condition.threshold == 70.0
