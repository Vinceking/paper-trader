"""Indicator pipeline tests. BUILD_SPEC §8.4.

Standard indicators (EMA/SMA/RSI/MACD/ATR/Bollinger/ADX) come from
pandas-ta-classic — these tests check the wrapper's warm-up/None handling,
not re-derive the library's own math. The hand-rolled session/microstructure
fields (VWAP+bands, opening range, gap, minutes_since_open, regime,
spread_bps) get correctness checks against hand-computed values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.ingest.bars import FinalBar
from app.strategies.indicators import SpreadQuote, compute_indicators

SESSION_OPEN_UTC = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)  # 09:30 ET


def bar(minutes: int, o, h, low, c, v, timeframe="1Min") -> FinalBar:
    return FinalBar(
        symbol="XLF", timeframe=timeframe, ts=SESSION_OPEN_UTC + timedelta(minutes=minutes),
        open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(low)), close=Decimal(str(c)),
        volume=v, vwap=None, trade_count=1,
    )


def flat_series(n: int, price: float = 100.0, vol: int = 1000) -> list[FinalBar]:
    """Perfectly flat OHLC — zero dispersion, zero directional movement.

    Only useful for testing things that should collapse to a single value
    under zero dispersion (VWAP bands). RSI/ADX are mathematically
    undefined (0/0) on a truly flat series, so warm-up/regime tests use
    `choppy_series` instead, which has real (but directionless) movement.
    """
    return [bar(i, price, price + 0.1, price - 0.1, price, vol) for i in range(n)]


def choppy_series(n: int, price: float = 100.0, vol: int = 1000) -> list[FinalBar]:
    """Small alternating up/down movement with no net direction — real
    price change bar-to-bar (so RSI/ADX are well-defined), but no trend."""
    bars = []
    for i in range(n):
        c = price + (0.3 if i % 2 == 0 else -0.3)
        bars.append(bar(i, price, c + 0.1, c - 0.1, c, vol))
    return bars


class TestWarmup:
    def test_short_history_returns_none_for_long_indicators(self):
        snap = compute_indicators(flat_series(5))
        assert snap.ema_200 is None
        assert snap.sma_200 is None
        assert snap.bb_basis is None

    def test_enough_history_populates_short_indicators(self):
        snap = compute_indicators(choppy_series(30))
        assert snap.ema_9 is not None
        assert snap.sma_5 is not None
        assert snap.rsi_2 is not None


class TestSessionVwap:
    def test_matches_hand_computed_volume_weighted_average(self):
        # typical price = (h+l+c)/3; two bars, weight by volume.
        bars = [
            bar(0, 100, 100.3, 99.9, 100.1, 100),   # typical = 100.1
            bar(1, 100.1, 100.5, 100.0, 100.2, 300),  # typical ~= 100.233333
        ]
        snap = compute_indicators(bars)
        # vwap = (100.1*100 + 100.23333...*300) / 400
        typical0 = (100.3 + 99.9 + 100.1) / 3  # (high + low + close) / 3
        typical1 = (100.5 + 100.0 + 100.2) / 3
        expected_vwap = (typical0 * 100 + typical1 * 300) / 400
        assert snap.vwap is not None
        assert abs(snap.vwap - expected_vwap) < 1e-9

    def test_bands_widen_with_dispersion(self):
        bars = flat_series(10)
        snap = compute_indicators(bars)
        # Flat prices -> zero dispersion -> bands collapse onto vwap.
        assert snap.vwap_upper_1 == snap.vwap
        assert snap.vwap_lower_1 == snap.vwap

    def test_daily_bars_have_no_session_vwap(self):
        bars = [
            bar(i * 1440, 100 + i, 101 + i, 99 + i, 100 + i, 1000, timeframe="1Day")
            for i in range(5)
        ]
        snap = compute_indicators(bars)
        assert snap.vwap is None


class TestOpeningRange:
    def test_high_low_of_first_15_minutes_only(self):
        bars = [
            bar(0, 100, 101, 99, 100, 100),
            bar(10, 100, 105, 98, 100, 100),   # inside first 15 min -> counts
            bar(20, 100, 110, 90, 100, 100),   # outside -> must NOT count
        ]
        snap = compute_indicators(bars)
        assert snap.opening_range_high == 105
        assert snap.opening_range_low == 98

    def test_widens_as_more_bars_arrive_within_the_window(self):
        early = compute_indicators([bar(0, 100, 101, 99, 100, 100)])
        later = compute_indicators([
            bar(0, 100, 101, 99, 100, 100),
            bar(5, 100, 103, 97, 100, 100),
        ])
        assert later.opening_range_high == 103
        assert later.opening_range_low == 97
        assert early.opening_range_high == 101


class TestGapPct:
    def test_gap_computed_from_prior_session_close(self):
        # 24h earlier -> prior trading day, close 99.0
        prev_session = [bar(-1440, 99, 99.5, 98.5, 99.0, 500)]
        today_open = [bar(0, 100.98, 101, 100.9, 101, 500)]     # today's open 100.98
        snap = compute_indicators(prev_session + today_open)
        # gap_pct = (100.98 - 99.0) / 99.0 * 100
        expected = (100.98 - 99.0) / 99.0 * 100
        assert snap.gap_pct is not None
        assert abs(snap.gap_pct - expected) < 1e-6

    def test_no_prior_session_yields_none(self):
        snap = compute_indicators([bar(0, 100, 101, 99, 100, 500)])
        assert snap.gap_pct is None


class TestMinutesSinceOpen:
    def test_matches_elapsed_time(self):
        snap = compute_indicators([bar(0, 100, 101, 99, 100, 500), bar(37, 100, 101, 99, 100, 500)])
        assert snap.minutes_since_open == 37


class TestSpreadBps:
    def test_none_without_a_quote(self):
        snap = compute_indicators(flat_series(5))
        assert snap.spread_bps is None

    def test_computed_from_quote_when_supplied(self):
        snap = compute_indicators(
            flat_series(5), quote=SpreadQuote(bid=Decimal("99.98"), ask=Decimal("100.02")),
        )
        # mid = 100.00, spread = 0.04 -> 4 bps
        assert snap.spread_bps is not None
        assert abs(snap.spread_bps - 4.0) < 1e-6


class TestRegime:
    def test_flat_low_adx_series_is_chop(self):
        snap = compute_indicators(choppy_series(60))
        assert snap.regime == "chop"

    def test_strong_uptrend_is_trend_up(self):
        bars = [bar(i, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i, 1000) for i in range(60)]
        snap = compute_indicators(bars)
        assert snap.regime == "trend_up"

    def test_strong_downtrend_is_trend_down(self):
        bars = [bar(i, 100 - i, 100 - i + 0.5, 100 - i - 0.5, 100 - i, 1000) for i in range(60)]
        snap = compute_indicators(bars)
        assert snap.regime == "trend_down"

    def test_too_little_history_is_unknown(self):
        snap = compute_indicators(flat_series(5))
        assert snap.regime is None
