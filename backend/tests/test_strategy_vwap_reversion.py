"""Tests for the VWAP Mean Reversion strategy. BUILD_SPEC §8.3 item 2.

Builds `BarContext`/`IndicatorSnapshot`/`OpenPosition` directly (per the
`test_engine.py` pattern) rather than going through the full indicator
pipeline — these tests are about the strategy's decision logic, not
indicator math (that's `test_indicators.py`'s job).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.execution.positions import OpenPosition
from app.ingest.bars import FinalBar
from app.strategies.base import BarContext
from app.strategies.indicators import IndicatorSnapshot
from app.strategies.vwap_reversion import (
    RULE_ID_ENTRY,
    RULE_ID_EXIT_STOP,
    RULE_ID_EXIT_VWAP_TOUCH,
    VwapReversionStrategy,
)

START = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)

_INDICATOR_FIELDS = [
    "ema_9", "ema_21", "ema_50", "ema_200",
    "sma_5", "sma_20", "sma_200",
    "rsi_2", "rsi_14",
    "macd_line", "macd_signal", "macd_hist",
    "atr_14",
    "bb_basis", "bb_upper", "bb_lower",
    "vwap", "vwap_upper_1", "vwap_lower_1", "vwap_upper_2", "vwap_lower_2",
    "volume_zscore_20", "relative_volume_20",
    "opening_range_high", "opening_range_low",
    "gap_pct", "spread_bps", "minutes_since_open",
    "adx_14", "regime",
]


def make_indicators(**overrides) -> IndicatorSnapshot:
    values = {name: None for name in _INDICATOR_FIELDS}
    values.update(overrides)
    return IndicatorSnapshot(**values)


def make_bar(minutes: int, price: float, symbol="AAPL") -> FinalBar:
    return FinalBar(
        symbol=symbol, timeframe="1Min", ts=START + timedelta(minutes=minutes),
        open=Decimal(str(price)), high=Decimal(str(price + 0.1)),
        low=Decimal(str(price - 0.1)), close=Decimal(str(price)),
        volume=1000, vwap=None, trade_count=1,
    )


def make_ctx(
    price: float,
    indicators: IndicatorSnapshot,
    symbol="AAPL",
    daily_ema_200: float | None = None,
) -> BarContext:
    bar = make_bar(0, price, symbol=symbol)
    daily_indicators = (
        make_indicators(ema_200=daily_ema_200) if daily_ema_200 is not None else None
    )
    return BarContext(
        symbol=symbol, bar=bar, history=[bar], indicators=indicators,
        daily_history=[bar] if daily_indicators is not None else None,
        daily_indicators=daily_indicators,
    )


def make_position(
    avg_entry_price: float, stop_price: float | None, side="buy"
) -> OpenPosition:
    return OpenPosition(
        symbol="AAPL", side=side, qty=Decimal("10"),
        avg_entry_price=Decimal(str(avg_entry_price)),
        stop_price=None if stop_price is None else Decimal(str(stop_price)),
        opened_at=START,
    )


# Session VWAP=100, ±1 std band at 99/101 -> std=1.0, so vwap_lower_2 = 98.
# Default k_std=2.0 -> oversold threshold price is 98.0.
BASE_INDICATORS = dict(vwap=100.0, vwap_upper_1=101.0, vwap_lower_1=99.0,
                        vwap_upper_2=102.0, vwap_lower_2=98.0, atr_14=1.0)


class TestEvaluateEntry:
    def test_fires_when_oversold_and_above_ema200(self):
        strat = VwapReversionStrategy()
        ind = make_indicators(**BASE_INDICATORS)
        ctx = make_ctx(97.5, ind, daily_ema_200=95.0)

        signal = strat.evaluate(ctx)

        assert signal is not None
        assert signal.side == "buy"
        assert signal.intent == "entry"
        assert signal.rule_id == RULE_ID_ENTRY
        # stop_price = entry - 1.5 * ATR(14) = 97.5 - 1.5*1.0 = 96.0
        assert signal.stop_price == Decimal("96.0")
        assert signal.target_price == Decimal("100.0")

        by_name = {c.name: c for c in signal.conditions}
        assert len(signal.conditions) == 2
        assert by_name["vwap_oversold"].passed is True
        assert by_name["vwap_oversold"].threshold == 98.0
        assert by_name["vwap_oversold"].actual == 97.5
        assert by_name["trend_filter_up"].passed is True
        assert by_name["trend_filter_up"].threshold == 95.0
        assert by_name["trend_filter_up"].actual == 97.5

    def test_no_entry_when_trend_filter_fails_but_conditions_show_detail(self):
        strat = VwapReversionStrategy()
        # Still oversold (97.5 < 98 threshold) but EMA200 above price -> downtrend.
        ind = make_indicators(**BASE_INDICATORS)
        ctx = make_ctx(97.5, ind, daily_ema_200=100.0)

        signal = strat.evaluate(ctx)
        assert signal is None

        conditions = strat._entry_conditions(ctx)
        by_name = {c.name: c for c in conditions}
        assert by_name["vwap_oversold"].passed is True
        assert by_name["trend_filter_up"].passed is False
        assert by_name["trend_filter_up"].threshold == 100.0
        assert by_name["trend_filter_up"].actual == 97.5

    def test_no_entry_when_not_far_enough_below_vwap(self):
        strat = VwapReversionStrategy()
        # Above EMA200 (trend ok) but only just under VWAP, not 2 std below.
        ind = make_indicators(**BASE_INDICATORS)
        ctx = make_ctx(99.0, ind, daily_ema_200=95.0)

        signal = strat.evaluate(ctx)
        assert signal is None

        conditions = strat._entry_conditions(ctx)
        by_name = {c.name: c for c in conditions}
        assert by_name["vwap_oversold"].passed is False
        assert by_name["trend_filter_up"].passed is True

    def test_no_entry_without_stop_distance_even_if_conditions_pass(self):
        """CLAUDE.md rule 6: an entry with no derivable stop must not fire."""
        strat = VwapReversionStrategy()
        ind = make_indicators(
            vwap=100.0, vwap_upper_1=101.0, vwap_lower_1=99.0,
            vwap_upper_2=102.0, vwap_lower_2=98.0, atr_14=None,
        )
        ctx = make_ctx(97.5, ind, daily_ema_200=95.0)

        assert strat.evaluate(ctx) is None

    def test_stop_price_never_none_on_any_entry_signal(self):
        strat = VwapReversionStrategy()
        scenarios = [
            (97.5, 95.0),
            (90.0, 50.0),
            (97.9, 10.0),
        ]
        fired = 0
        for price, daily_ema_200 in scenarios:
            ind = make_indicators(**BASE_INDICATORS)
            ctx = make_ctx(price, ind, daily_ema_200=daily_ema_200)
            signal = strat.evaluate(ctx)
            if signal is not None:
                fired += 1
                assert signal.stop_price is not None
        assert fired > 0  # sanity: at least one scenario actually fired


class TestEvaluateGracefulNoneHandling:
    def test_none_vwap_returns_none_without_raising(self):
        strat = VwapReversionStrategy()
        ind = make_indicators(vwap=None, vwap_lower_1=None, atr_14=1.0)
        ctx = make_ctx(90.0, ind, daily_ema_200=95.0)
        assert strat.evaluate(ctx) is None

    def test_no_daily_indicators_returns_none_without_raising(self):
        """No daily bars fed to the engine yet for this symbol -> the daily
        trend filter can't be evaluated, so no entry -- not an exception."""
        strat = VwapReversionStrategy()
        ind = make_indicators(**BASE_INDICATORS)
        ctx = make_ctx(97.5, ind)  # daily_ema_200 not supplied -> daily_indicators=None
        assert strat.evaluate(ctx) is None

    def test_none_daily_ema200_returns_none_without_raising(self):
        """Daily bars exist but EMA(200) hasn't warmed up yet on them."""
        strat = VwapReversionStrategy()
        ind = make_indicators(**BASE_INDICATORS)
        ctx = make_ctx(97.5, ind)
        ctx = BarContext(
            symbol=ctx.symbol, bar=ctx.bar, history=ctx.history, indicators=ctx.indicators,
            daily_history=[ctx.bar], daily_indicators=make_indicators(ema_200=None),
        )
        assert strat.evaluate(ctx) is None

    def test_none_vwap_lower_1_returns_none_without_raising(self):
        """No vwap_lower_1 means std can't be derived (see indicators.py note
        about there being no directly-exposed vwap-std field)."""
        strat = VwapReversionStrategy()
        ind = make_indicators(vwap=100.0, vwap_lower_1=None, atr_14=1.0)
        ctx = make_ctx(97.5, ind, daily_ema_200=95.0)
        assert strat.evaluate(ctx) is None


class TestManage:
    def test_vwap_touch_exit_fires(self):
        strat = VwapReversionStrategy()
        position = make_position(avg_entry_price=97.5, stop_price=96.0)
        ind = make_indicators(vwap=100.0)
        ctx = make_ctx(100.5, ind)  # close >= vwap

        signal = strat.manage(ctx, position)

        assert signal is not None
        assert signal.side == "sell"
        assert signal.intent == "exit"
        assert signal.rule_id == RULE_ID_EXIT_VWAP_TOUCH
        by_name = {c.name: c for c in signal.conditions}
        assert by_name["vwap_touch"].passed is True

    def test_atr_stop_exit_fires(self):
        strat = VwapReversionStrategy()
        position = make_position(avg_entry_price=97.5, stop_price=96.0)
        ind = make_indicators(vwap=105.0)  # nowhere near a vwap touch
        ctx = make_ctx(95.5, ind)  # close <= stop_price(96.0)

        signal = strat.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == RULE_ID_EXIT_STOP
        by_name = {c.name: c for c in signal.conditions}
        assert by_name["atr_stop_hit"].passed is True
        assert by_name["vwap_touch"].passed is False

    def test_stop_takes_priority_when_both_conditions_hold(self):
        strat = VwapReversionStrategy()
        position = make_position(avg_entry_price=97.5, stop_price=96.0)
        ind = make_indicators(vwap=95.0)  # close(95.5) >= vwap(95.0) too
        ctx = make_ctx(95.5, ind)

        signal = strat.manage(ctx, position)

        assert signal is not None
        assert signal.rule_id == RULE_ID_EXIT_STOP

    def test_no_exit_when_neither_condition_holds(self):
        strat = VwapReversionStrategy()
        position = make_position(avg_entry_price=97.5, stop_price=96.0)
        ind = make_indicators(vwap=100.0)
        ctx = make_ctx(97.0, ind)  # between stop(96) and vwap(100)

        assert strat.manage(ctx, position) is None

    def test_no_exit_when_position_stop_price_is_none(self):
        """Only the VWAP-touch check applies if a position somehow has no
        recorded stop; the stop-hit condition is simply absent, not an
        error."""
        strat = VwapReversionStrategy()
        position = make_position(avg_entry_price=97.5, stop_price=None)
        ind = make_indicators(vwap=100.0)
        ctx = make_ctx(97.0, ind)  # doesn't touch vwap

        assert strat.manage(ctx, position) is None

    def test_no_exit_when_indicators_and_stop_both_unavailable(self):
        strat = VwapReversionStrategy()
        position = make_position(avg_entry_price=97.5, stop_price=None)
        ind = make_indicators(vwap=None)
        ctx = make_ctx(97.0, ind)

        assert strat.manage(ctx, position) is None
