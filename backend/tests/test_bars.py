"""Phase 1 acceptance tests for bar construction and gap detection.

BUILD_SPEC §16 Phase 1 and §18.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.ingest.bars import BarBuilder, detect_gaps, floor_to_timeframe


def ts(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 8, 28, hh, mm, ss, tzinfo=timezone.utc)


class TestFloorToTimeframe:
    def test_floors_to_minute(self):
        assert floor_to_timeframe(ts(14, 31, 47), "1Min") == ts(14, 31, 0)

    def test_floors_to_five_minutes(self):
        assert floor_to_timeframe(ts(14, 33, 12), "5Min") == ts(14, 30, 0)

    def test_exact_boundary_is_its_own_bar(self):
        assert floor_to_timeframe(ts(14, 30, 0), "5Min") == ts(14, 30, 0)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            floor_to_timeframe(datetime(2026, 8, 28, 14, 30), "1Min")

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(ValueError, match="unknown timeframe"):
            floor_to_timeframe(ts(14, 30), "3Min")


class TestBarBuilder:
    def test_single_trade_starts_working_bar_and_finalizes_nothing(self):
        b = BarBuilder("SPY")
        assert b.on_trade(ts(14, 30, 5), Decimal("100.00"), 10) is None
        assert b.working is not None
        assert b.working.ts == ts(14, 30, 0)

    def test_ohlc_is_correct(self):
        b = BarBuilder("SPY")
        for price, size in [("100.00", 10), ("102.50", 5), ("99.25", 20), ("101.00", 1)]:
            b.on_trade(ts(14, 30, 10), Decimal(price), size)
        w = b.working
        assert w.open == Decimal("100.00")
        assert w.high == Decimal("102.50")
        assert w.low == Decimal("99.25")
        assert w.close == Decimal("101.00")
        assert w.volume == 36
        assert w.trade_count == 4

    def test_next_minute_tick_finalizes_previous_bar(self):
        b = BarBuilder("SPY")
        b.on_trade(ts(14, 30, 5), Decimal("100.00"), 10)
        final = b.on_trade(ts(14, 31, 1), Decimal("101.00"), 5)
        assert final is not None
        assert final.ts == ts(14, 30, 0)
        assert final.close == Decimal("100.00")
        # and a new bar is now forming
        assert b.working.ts == ts(14, 31, 0)

    def test_vwap_is_volume_weighted(self):
        b = BarBuilder("SPY")
        b.on_trade(ts(14, 30, 1), Decimal("100.00"), 100)
        b.on_trade(ts(14, 30, 2), Decimal("200.00"), 300)
        # (100*100 + 200*300) / 400 = 175
        assert b.working.vwap == Decimal("175.0000")

    def test_wall_clock_finalizes_thin_symbol(self):
        """A symbol that prints nothing next minute must still close its bar."""
        b = BarBuilder("KRE", grace_seconds=2.0)
        b.on_trade(ts(14, 30, 5), Decimal("50.00"), 1)
        assert b.maybe_finalize(ts(14, 31, 1)) is None      # inside grace
        final = b.maybe_finalize(ts(14, 31, 3))             # past grace
        assert final is not None and final.ts == ts(14, 30, 0)

    def test_late_tick_does_not_mutate_closed_bar(self):
        """Out-of-order ticks are dropped, never backfilled into history.

        A rewritten bar silently invalidates every indicator computed from it.
        """
        b = BarBuilder("SPY")
        b.on_trade(ts(14, 30, 5), Decimal("100.00"), 10)
        b.on_trade(ts(14, 31, 5), Decimal("101.00"), 10)   # closes 14:30
        result = b.on_trade(ts(14, 30, 55), Decimal("999.00"), 1)  # late straggler
        assert result is None
        assert b.working.high == Decimal("101.00")         # untouched

    def test_no_finalize_when_nothing_working(self):
        assert BarBuilder("SPY").maybe_finalize(ts(23, 0)) is None


class TestGapDetection:
    def test_no_gaps_when_complete(self):
        have = [ts(14, 30) + timedelta(minutes=i) for i in range(5)]
        gaps = detect_gaps("SPY", "1Min", have, ts(14, 30), ts(14, 35))
        assert gaps == []

    def test_single_missing_bar(self):
        have = [ts(14, 30), ts(14, 31), ts(14, 33), ts(14, 34)]
        gaps = detect_gaps("SPY", "1Min", have, ts(14, 30), ts(14, 35))
        assert len(gaps) == 1
        assert gaps[0].start == ts(14, 32)
        assert gaps[0].expected_bars == 1

    def test_contiguous_gap_is_one_event(self):
        have = [ts(14, 30), ts(14, 35)]
        gaps = detect_gaps("SPY", "1Min", have, ts(14, 30), ts(14, 36))
        assert len(gaps) == 1
        assert gaps[0].start == ts(14, 31)
        assert gaps[0].expected_bars == 4

    def test_two_separate_gaps(self):
        have = [ts(14, 30), ts(14, 32), ts(14, 35)]
        gaps = detect_gaps("SPY", "1Min", have, ts(14, 30), ts(14, 36))
        assert [g.expected_bars for g in gaps] == [1, 2]

    def test_trailing_gap_detected(self):
        """The most dangerous gap: the feed died and never came back."""
        have = [ts(14, 30), ts(14, 31)]
        gaps = detect_gaps("SPY", "1Min", have, ts(14, 30), ts(14, 40))
        assert len(gaps) == 1
        assert gaps[0].start == ts(14, 32)
        assert gaps[0].expected_bars == 8

    def test_empty_session_is_one_big_gap(self):
        gaps = detect_gaps("SPY", "1Min", [], ts(14, 30), ts(14, 35))
        assert len(gaps) == 1 and gaps[0].expected_bars == 5


class TestBarBuilderProperties:
    @given(
        prices=st.lists(
            st.decimals(min_value=1, max_value=1000, places=2),
            min_size=1, max_size=50,
        )
    )
    def test_high_always_gte_low_and_contains_open_close(self, prices):
        b = BarBuilder("SPY")
        for p in prices:
            b.on_trade(ts(14, 30, 1), Decimal(p), 1)
        w = b.working
        assert w.high >= w.low
        assert w.low <= w.open <= w.high
        assert w.low <= w.close <= w.high

    @given(sizes=st.lists(st.integers(min_value=1, max_value=10_000),
                          min_size=1, max_size=50))
    def test_volume_is_sum_of_sizes(self, sizes):
        b = BarBuilder("SPY")
        for s in sizes:
            b.on_trade(ts(14, 30, 1), Decimal("100.00"), s)
        assert b.working.volume == sum(sizes)
