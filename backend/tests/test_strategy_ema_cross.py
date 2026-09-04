"""EMA Crossover with regime filter strategy tests. BUILD_SPEC §8.3 #3.

The crossover check needs a real, previous-bar-vs-current-bar EMA9/EMA21
cross to test honestly (rather than a hand-built IndicatorSnapshot that
could hide a bug in how the strategy recomputes the previous bar's
values), so entry tests build a real 5-minute bar series and use the
actual `compute_indicators` pipeline -- the same function the strategy
itself calls -- to locate a genuine fresh cross and a genuine
already-crossed point within it.

`manage()` doesn't recompute indicators internally (it only reads
`ctx.indicators.atr_14`), so those tests hand-build an `IndicatorSnapshot`
directly to pin ATR to an exact, known value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.execution.positions import OpenPosition
from app.ingest.bars import FinalBar
from app.strategies.base import BarContext
from app.strategies.ema_cross import EmaCrossoverRegimeFilter
from app.strategies.indicators import IndicatorSnapshot, compute_indicators

START = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)


def bar5(i: int, close: float, symbol="AAPL") -> FinalBar:
    """A 5-minute FinalBar. High/low bracket the close by a small margin,
    matching the fixture pattern in tests/test_indicators.py."""
    c = Decimal(str(close))
    return FinalBar(
        symbol=symbol, timeframe="5Min", ts=START + timedelta(minutes=5 * i),
        open=c, high=c + Decimal("0.1"), low=c - Decimal("0.1"), close=c,
        volume=1000, vwap=None, trade_count=1,
    )


def bar_ohlc(i: int, o: float, h: float, low: float, c: float, symbol="AAPL") -> FinalBar:
    """A 5-minute FinalBar with fully explicit OHLC, for manage() tests that
    need exact control over high/low relative to a stop/1R threshold."""
    return FinalBar(
        symbol=symbol, timeframe="5Min", ts=START + timedelta(minutes=5 * i),
        open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(low)),
        close=Decimal(str(c)), volume=1000, vwap=None, trade_count=1,
    )


def daily_bar(i: int, close: float, symbol="AAPL") -> FinalBar:
    c = Decimal(str(close))
    return FinalBar(
        symbol=symbol, timeframe="1Day", ts=START + timedelta(days=i),
        open=c, high=c + Decimal("0.2"), low=c - Decimal("0.2"), close=c,
        volume=500_000, vwap=None, trade_count=1,
    )


def uptrend_daily_bars(n: int = 200) -> list[FinalBar]:
    """200 daily bars trending from 90 up to ~110 -- close ends comfortably
    above the 200-day SMA (~100), so the regime filter passes."""
    return [daily_bar(i, 90 + (20 * i / (n - 1))) for i in range(n)]


def downtrend_daily_bars(n: int = 200) -> list[FinalBar]:
    """200 daily bars trending from 110 down to ~90 -- close ends below the
    200-day SMA (~100), so the regime filter fails."""
    return [daily_bar(i, 110 - (20 * i / (n - 1))) for i in range(n)]


def crossing_5min_series(n: int = 90) -> list[FinalBar]:
    """A 5-minute series that dips for 40 bars then climbs steadily, so a
    real EMA(9)/EMA(21) golden cross happens somewhere in the climb."""
    bars = []
    price = 150.0
    for i in range(40):
        price -= 1.0
        bars.append(bar5(i, price))
    for i in range(40, n):
        price += 1.5
        bars.append(bar5(i, price))
    return bars


def find_fresh_cross_index(bars: list[FinalBar]) -> int:
    """The index (0-based, into `bars`) of the first bar at which EMA9
    freshly crosses above EMA21 -- computed via the real indicator
    pipeline, the same one the strategy itself calls."""
    for i in range(25, len(bars) - 1):
        prev_snap = compute_indicators(bars[: i + 1])
        cur_snap = compute_indicators(bars[: i + 2])
        if (
            prev_snap.ema_9 is not None
            and prev_snap.ema_21 is not None
            and cur_snap.ema_9 is not None
            and cur_snap.ema_21 is not None
            and prev_snap.ema_9 <= prev_snap.ema_21
            and cur_snap.ema_9 > cur_snap.ema_21
        ):
            return i + 1
    raise AssertionError("no fresh EMA9/EMA21 cross found in synthetic series")


def make_snapshot(**overrides) -> IndicatorSnapshot:
    """An IndicatorSnapshot with every field None except what's overridden --
    for manage() tests, which only read atr_14."""
    fields = {
        "ema_9": None, "ema_21": None, "ema_50": None, "ema_200": None,
        "sma_5": None, "sma_20": None, "sma_200": None,
        "rsi_2": None, "rsi_14": None,
        "macd_line": None, "macd_signal": None, "macd_hist": None,
        "atr_14": None,
        "bb_basis": None, "bb_upper": None, "bb_lower": None,
        "vwap": None, "vwap_upper_1": None, "vwap_lower_1": None,
        "vwap_upper_2": None, "vwap_lower_2": None,
        "volume_zscore_20": None, "relative_volume_20": None,
        "opening_range_high": None, "opening_range_low": None,
        "gap_pct": None, "spread_bps": None, "minutes_since_open": None,
        "adx_14": None, "regime": None,
    }
    fields.update(overrides)
    return IndicatorSnapshot(**fields)


class TestEntryFiresOnFreshCrossWithRegimeUp:
    def test_entry_fires_with_stop_and_passing_conditions(self):
        strat = EmaCrossoverRegimeFilter()
        bars = crossing_5min_series()
        cross_idx = find_fresh_cross_index(bars)
        history = bars[: cross_idx + 1]

        ctx = BarContext(
            symbol="AAPL",
            bar=history[-1],
            history=history,
            indicators=compute_indicators(history),
            daily_history=uptrend_daily_bars(),
            daily_indicators=compute_indicators(uptrend_daily_bars()),
        )

        signal = strat.evaluate(ctx)

        assert signal is not None
        assert signal.side == "buy"
        assert signal.intent == "entry"
        assert signal.rule_id == "ema_cross.golden_cross_long"
        assert signal.stop_price is not None  # CLAUDE.md rule 6

        by_name = {c.name: c for c in signal.conditions}
        assert by_name["ema9_not_above_ema21_prior_bar"].passed is True
        assert by_name["ema9_above_ema21_now"].passed is True
        assert by_name["daily_close_above_sma_200"].passed is True

    def test_stop_price_is_never_none_on_entry(self):
        strat = EmaCrossoverRegimeFilter()
        bars = crossing_5min_series()
        cross_idx = find_fresh_cross_index(bars)
        history = bars[: cross_idx + 1]

        ctx = BarContext(
            symbol="AAPL",
            bar=history[-1],
            history=history,
            indicators=compute_indicators(history),
            daily_history=uptrend_daily_bars(),
            daily_indicators=compute_indicators(uptrend_daily_bars()),
        )

        signal = strat.evaluate(ctx)
        assert signal is not None
        assert signal.stop_price is not None
        assert isinstance(signal.stop_price, Decimal)


class TestNoEntryWhenNotAFreshCross:
    def test_already_crossed_earlier_fails_the_crossover_condition(self):
        """A few bars after the actual cross, EMA9 > EMA21 on BOTH the
        current and the previous bar -- the trend is continuing, not
        freshly crossing. The 'above now' check alone would pass, but the
        'not already above on the prior bar' check must fail, and evaluate()
        must return None."""
        strat = EmaCrossoverRegimeFilter()
        bars = crossing_5min_series()
        cross_idx = find_fresh_cross_index(bars)
        later_idx = cross_idx + 5
        history = bars[: later_idx + 1]

        ctx = BarContext(
            symbol="AAPL",
            bar=history[-1],
            history=history,
            indicators=compute_indicators(history),
            daily_history=uptrend_daily_bars(),
            daily_indicators=compute_indicators(uptrend_daily_bars()),
        )

        not_crossed_prior, above_now = strat._crossover_conditions(ctx)
        assert above_now.passed is True
        assert not_crossed_prior.passed is False

        assert strat.evaluate(ctx) is None


class TestNoEntryWhenRegimeFails:
    def test_crossover_passes_but_daily_regime_fails(self):
        strat = EmaCrossoverRegimeFilter()
        bars = crossing_5min_series()
        cross_idx = find_fresh_cross_index(bars)
        history = bars[: cross_idx + 1]

        ctx = BarContext(
            symbol="AAPL",
            bar=history[-1],
            history=history,
            indicators=compute_indicators(history),
            daily_history=downtrend_daily_bars(),
            daily_indicators=compute_indicators(downtrend_daily_bars()),
        )

        not_crossed_prior, above_now = strat._crossover_conditions(ctx)
        regime = strat._regime_condition(ctx)
        assert not_crossed_prior.passed is True
        assert above_now.passed is True
        assert regime.passed is False

        assert strat.evaluate(ctx) is None


class TestNoEntryWithoutDailyContext:
    def test_no_daily_indicators_returns_none_gracefully(self):
        strat = EmaCrossoverRegimeFilter()
        bars = crossing_5min_series()
        cross_idx = find_fresh_cross_index(bars)
        history = bars[: cross_idx + 1]

        ctx = BarContext(
            symbol="AAPL",
            bar=history[-1],
            history=history,
            indicators=compute_indicators(history),
            daily_history=None,
            daily_indicators=None,
        )

        # Must not raise, must simply not fire.
        assert strat.evaluate(ctx) is None


class TestInsufficientHistoryForCrossover:
    def test_fewer_than_two_bars_treated_as_failed_not_raised(self):
        strat = EmaCrossoverRegimeFilter()
        only_bar = [bar5(0, 100.0)]

        ctx = BarContext(
            symbol="AAPL",
            bar=only_bar[-1],
            history=only_bar,
            indicators=compute_indicators(only_bar),
            daily_history=uptrend_daily_bars(),
            daily_indicators=compute_indicators(uptrend_daily_bars()),
        )

        # No exception, and no signal (nowhere near enough EMA warm-up either).
        assert strat.evaluate(ctx) is None


class TestManageStopHit:
    def test_stop_hit_before_1r_reached(self):
        strat = EmaCrossoverRegimeFilter()
        position = OpenPosition(
            symbol="AAPL", side="buy", qty=Decimal("10"),
            avg_entry_price=Decimal("100"), stop_price=Decimal("95"),
            opened_at=START,
        )
        # Never trades up to the 1R price of 105.
        history = [
            bar_ohlc(0, 100, 100.5, 99.5, 100),
            bar_ohlc(1, 100, 101, 97, 97),
            bar_ohlc(2, 97, 98, 94, 94.5),  # low breaches the 95 stop
        ]
        ctx = BarContext(
            symbol="AAPL", bar=history[-1], history=history,
            indicators=make_snapshot(atr_14=1.0),
        )

        signal = strat.manage(ctx, position)

        assert signal is not None
        assert signal.side == "sell"
        assert signal.intent == "exit"
        assert signal.rule_id == "ema_cross.stop_hit"

    def test_no_exit_while_price_stays_above_stop(self):
        strat = EmaCrossoverRegimeFilter()
        position = OpenPosition(
            symbol="AAPL", side="buy", qty=Decimal("10"),
            avg_entry_price=Decimal("100"), stop_price=Decimal("95"),
            opened_at=START,
        )
        history = [
            bar_ohlc(0, 100, 100.5, 99.5, 100),
            bar_ohlc(1, 100, 101, 98, 100.5),
        ]
        ctx = BarContext(
            symbol="AAPL", bar=history[-1], history=history,
            indicators=make_snapshot(atr_14=1.0),
        )

        assert strat.manage(ctx, position) is None


class TestManageTrailStopAfter1R:
    def test_trail_documented_rule_max_of_close_minus_1_5_atr_since_1r(self):
        """entry=100, stop=95 -> 1R = 105. Bar[1]'s high (106) reaches 1R.
        ATR is pinned to 2.0, trail_atr_mult defaults to 1.5, so the trail
        amount is 3.0. Trail candidates from bar[1] onward:
          bar[1].close=105 -> 102
          bar[2].close=107 -> 104   <- the running max
          bar[3].close=91  -> 88
        effective_stop = max(initial_stop=95, 104) = 104. bar[3].low=90
        breaches that trailed stop -> a trail exit, not a plain stop exit."""
        strat = EmaCrossoverRegimeFilter()
        position = OpenPosition(
            symbol="AAPL", side="buy", qty=Decimal("10"),
            avg_entry_price=Decimal("100"), stop_price=Decimal("95"),
            opened_at=START,
        )
        history = [
            bar_ohlc(0, 100, 100, 100, 100),
            bar_ohlc(1, 100, 106, 99, 105),
            bar_ohlc(2, 105, 108, 103, 107),
            bar_ohlc(3, 107, 109, 90, 91),
        ]
        ctx = BarContext(
            symbol="AAPL", bar=history[-1], history=history,
            indicators=make_snapshot(atr_14=2.0),
        )

        signal = strat.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == "ema_cross.trail_stop"
        assert signal.features["effective_stop"] == 104.0
        assert signal.features["reached_1r"] is True

    def test_no_exit_when_trailed_stop_not_yet_breached(self):
        strat = EmaCrossoverRegimeFilter()
        position = OpenPosition(
            symbol="AAPL", side="buy", qty=Decimal("10"),
            avg_entry_price=Decimal("100"), stop_price=Decimal("95"),
            opened_at=START,
        )
        history = [
            bar_ohlc(0, 100, 100, 100, 100),
            bar_ohlc(1, 100, 106, 99, 105),  # reaches 1R
            bar_ohlc(2, 105, 108, 106, 107),  # stays well above the trail
        ]
        ctx = BarContext(
            symbol="AAPL", bar=history[-1], history=history,
            indicators=make_snapshot(atr_14=2.0),
        )

        assert strat.manage(ctx, position) is None
