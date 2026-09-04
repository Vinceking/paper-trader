"""Opening Range Breakout strategy tests. BUILD_SPEC §8.3 #1, CLAUDE.md rules 2/4/6.

Builds `BarContext`/`IndicatorSnapshot` directly rather than going through
`SymbolEngine` — the indicator pipeline itself is covered by
test_indicators.py; these tests are about the strategy's own decision logic
(condition computation, entry/exit signals, stop/target/trail math).
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.execution.positions import OpenPosition
from app.ingest.bars import FinalBar
from app.market_calendar import NY
from app.strategies.base import BarContext
from app.strategies.indicators import IndicatorSnapshot
from app.strategies.orb import OpeningRangeBreakout

START = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)  # 09:30 ET


def make_bar(
    minutes: int,
    close: float,
    high: float | None = None,
    low: float | None = None,
    symbol: str = "AAPL",
    vol: int = 1000,
    ts: datetime | None = None,
) -> FinalBar:
    high = close if high is None else high
    low = close if low is None else low
    bar_ts = ts if ts is not None else START + timedelta(minutes=minutes)
    return FinalBar(
        symbol=symbol, timeframe="1Min", ts=bar_ts,
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
    stop: float = 99.0,
    opened_at: datetime | None = None,
) -> OpenPosition:
    return OpenPosition(
        symbol="AAPL", side="buy", qty=Decimal("10"),
        avg_entry_price=Decimal(str(entry)), stop_price=Decimal(str(stop)),
        opened_at=opened_at if opened_at is not None else START,
    )


STRAT = OpeningRangeBreakout()


class TestEntryConditions:
    def test_entry_fires_when_breakout_and_both_filters_pass(self):
        # range width = 1.0, atr = 1.0 -> ratio 1.0 >= min 0.5 (passes)
        # relative volume 1.5 >= min 1.2 (passes); close 100.5 > range high 100 (passes)
        ind = make_indicators(
            opening_range_high=100.0, opening_range_low=99.0,
            atr_14=1.0, relative_volume_20=1.5,
        )
        bar = make_bar(20, close=100.5)
        ctx = make_ctx(bar, ind)

        signal = STRAT.evaluate(ctx)

        assert signal is not None
        assert signal.rule_id == "orb.breakout_long"
        assert signal.side == "buy"
        assert signal.intent == "entry"
        assert signal.stop_price == Decimal("99.0")
        # target = entry + 2R = 100.5 + 2*(100.5-99.0) = 103.5
        assert signal.target_price == Decimal("103.5")

        assert len(signal.conditions) == 3
        by_name = {c.name: c for c in signal.conditions}
        assert by_name["close_above_range_high"].passed is True
        assert by_name["close_above_range_high"].actual == 100.5
        assert by_name["close_above_range_high"].threshold == 100.0
        assert by_name["range_width_min_atr_mult"].passed is True
        assert by_name["range_width_min_atr_mult"].actual == 1.0
        assert by_name["range_width_min_atr_mult"].threshold == 0.5
        assert by_name["relative_volume_min"].passed is True
        assert by_name["relative_volume_min"].actual == 1.5
        assert by_name["relative_volume_min"].threshold == 1.2

    def test_no_entry_when_range_width_filter_fails_alone(self):
        # width = 0.1, atr = 1.0 -> ratio 0.1 < min 0.5 (fails)
        # breakout and volume both still pass.
        ind = make_indicators(
            opening_range_high=100.0, opening_range_low=99.9,
            atr_14=1.0, relative_volume_20=1.5,
        )
        bar = make_bar(20, close=100.5)
        ctx = make_ctx(bar, ind)

        conditions = STRAT._conditions(ctx)
        by_name = {c.name: c for c in conditions}
        assert by_name["close_above_range_high"].passed is True
        assert by_name["range_width_min_atr_mult"].passed is False
        assert by_name["relative_volume_min"].passed is True

        assert STRAT.evaluate(ctx) is None

    def test_no_entry_when_volume_filter_fails_alone(self):
        # relative volume 1.0 < min 1.2 (fails); breakout and width both pass.
        ind = make_indicators(
            opening_range_high=100.0, opening_range_low=99.0,
            atr_14=1.0, relative_volume_20=1.0,
        )
        bar = make_bar(20, close=100.5)
        ctx = make_ctx(bar, ind)

        conditions = STRAT._conditions(ctx)
        by_name = {c.name: c for c in conditions}
        assert by_name["close_above_range_high"].passed is True
        assert by_name["range_width_min_atr_mult"].passed is True
        assert by_name["relative_volume_min"].passed is False

        assert STRAT.evaluate(ctx) is None

    def test_no_entry_when_price_has_not_broken_range_high(self):
        ind = make_indicators(
            opening_range_high=100.0, opening_range_low=99.0,
            atr_14=1.0, relative_volume_20=1.5,
        )
        bar = make_bar(20, close=99.5)  # below range high
        ctx = make_ctx(bar, ind)

        conditions = STRAT._conditions(ctx)
        by_name = {c.name: c for c in conditions}
        assert by_name["close_above_range_high"].passed is False

        assert STRAT.evaluate(ctx) is None

    def test_no_entry_when_indicators_not_yet_available(self):
        ind = make_indicators()  # opening range / atr / rel vol all None (warm-up)
        bar = make_bar(2, close=100.5)
        ctx = make_ctx(bar, ind)

        assert STRAT._conditions(ctx) is None
        assert STRAT.evaluate(ctx) is None

    def test_entry_signal_stop_price_is_never_none(self):
        """CLAUDE.md rule 6: an entry Signal with stop_price=None gets
        rejected downstream by the risk engine — this strategy must never
        produce one."""
        scenarios = [
            (100.0, 99.0, 1.0, 1.5, 100.5),
            (50.0, 48.0, 2.0, 3.0, 51.0),
            (200.0, 199.0, 0.5, 1.2, 200.1),
        ]
        for or_high, or_low, atr, rel_vol, close in scenarios:
            ind = make_indicators(
                opening_range_high=or_high, opening_range_low=or_low,
                atr_14=atr, relative_volume_20=rel_vol,
            )
            bar = make_bar(20, close=close)
            ctx = make_ctx(bar, ind)
            signal = STRAT.evaluate(ctx)
            assert signal is not None
            assert signal.stop_price is not None


class TestManage:
    def _ctx_for(self, bar: FinalBar, history: list[FinalBar] | None = None) -> BarContext:
        ind = make_indicators()  # manage() doesn't need indicators
        return make_ctx(bar, ind, history=history)

    def test_stop_hit_before_1r_returns_stop_hit_exit(self):
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        bar = make_bar(1, close=98.7, high=100.5, low=98.5, ts=START + timedelta(minutes=1))
        ctx = self._ctx_for(bar, history=[bar])

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "orb.stop_hit"
        assert signal.intent == "exit"
        assert signal.side == "sell"

    def test_target_2r_hit_returns_target_exit(self):
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        # high 102.5 clears the 2R target (102); low 100.5 never dips back to
        # breakeven even though 1R was also reached in this same bar.
        bar = make_bar(1, close=102.0, high=102.5, low=100.5, ts=START + timedelta(minutes=1))
        ctx = self._ctx_for(bar, history=[bar])

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "orb.target_2r"
        assert signal.intent == "exit"

    def test_trailing_stop_after_1r_exits_at_breakeven(self):
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        bar_1r = make_bar(1, close=101.2, high=101.5, low=100.2, ts=START + timedelta(minutes=1))
        pullback_ts = START + timedelta(minutes=2)
        bar_pullback = make_bar(2, close=99.9, high=100.2, low=99.8, ts=pullback_ts)
        ctx = self._ctx_for(bar_pullback, history=[bar_1r, bar_pullback])

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "orb.trail_stop"
        assert signal.intent == "exit"

    def test_no_1r_yet_pullback_to_breakeven_does_not_trail(self):
        """Without ever reaching 1R, a dip back to entry price is not a stop
        touch (original stop is below entry) — manage() must not exit."""
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        bar = make_bar(1, close=100.0, high=100.3, low=99.9, ts=START + timedelta(minutes=1))
        ctx = self._ctx_for(bar, history=[bar])

        assert STRAT.manage(ctx, position) is None

    def test_time_exit_fires_at_1555_et_regardless_of_pnl(self):
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        # Price is comfortably between stop and target (no P&L-driven exit
        # would fire) but the bar is at 15:55 ET.
        ts = datetime(2026, 8, 31, 15, 55, tzinfo=NY)
        bar = make_bar(0, close=100.4, high=100.5, low=100.3, ts=ts)
        ctx = self._ctx_for(bar, history=[bar])

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "orb.time_exit"
        assert signal.intent == "exit"

    def test_time_exit_takes_priority_over_a_simultaneous_stop_hit(self):
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        ts = datetime(2026, 8, 31, 16, 0, tzinfo=NY)
        bar = make_bar(0, close=98.5, high=99.0, low=98.5, ts=ts)
        ctx = self._ctx_for(bar, history=[bar])

        signal = STRAT.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "orb.time_exit"

    def test_before_1555_et_no_time_exit(self):
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        ts = datetime(2026, 8, 31, 15, 54, tzinfo=NY)
        bar = make_bar(0, close=100.4, high=100.5, low=100.3, ts=ts)
        ctx = self._ctx_for(bar, history=[bar])

        assert STRAT.manage(ctx, position) is None

    def test_no_exit_when_price_between_stop_and_target(self):
        position = make_position(entry=100.0, stop=99.0, opened_at=START)
        bar = make_bar(1, close=100.4, high=100.5, low=100.3, ts=START + timedelta(minutes=1))
        ctx = self._ctx_for(bar, history=[bar])

        assert STRAT.manage(ctx, position) is None
