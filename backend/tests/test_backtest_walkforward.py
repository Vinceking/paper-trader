"""Walk-forward train/test splitting. BUILD_SPEC §8.5 rule 4."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.backtest.walkforward import rolling_splits, single_holdout_split
from app.ingest.bars import FinalBar


def _bars(n: int, start: datetime | None = None) -> list[FinalBar]:
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    return [
        FinalBar(
            symbol="SPY", timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100),
            volume=1000, vwap=None, trade_count=10,
        )
        for i in range(n)
    ]


class TestRollingSplits:
    def test_produces_expected_number_of_non_overlapping_folds(self):
        bars = _bars(100)
        splits = rolling_splits(bars, train_size=30, test_size=10)
        # step defaults to test_size (10): folds start at 0, 10, 20, ... and
        # each needs train_size+test_size=40 bars ending within 100 -> last
        # valid start is 60 (60+40=100) -> 7 folds (starts 0,10,...,60).
        assert len(splits) == 7
        assert len(splits[0].train) == 30
        assert len(splits[0].test) == 10

    def test_train_always_precedes_test_with_no_overlap(self):
        bars = _bars(100)
        splits = rolling_splits(bars, train_size=30, test_size=10)
        for split in splits:
            assert split.train[-1].ts < split.test[0].ts
            train_ts = {b.ts for b in split.train}
            test_ts = {b.ts for b in split.test}
            assert train_ts.isdisjoint(test_ts)

    def test_custom_step_advances_by_step_not_test_size(self):
        bars = _bars(100)
        splits = rolling_splits(bars, train_size=20, test_size=10, step=25)
        starts = [s.train[0].ts for s in splits]
        # Each fold's train start should be 25 bars (days) after the last.
        for a, b in pairwise(starts):
            assert (b - a).days == 25

    def test_trailing_partial_window_is_dropped(self):
        # 45 bars, train=30/test=10/step=10 -> fold0 uses [0:40), fold1 would
        # need [10:50) which exceeds 45 bars -> only fold0 is returned.
        bars = _bars(45)
        splits = rolling_splits(bars, train_size=30, test_size=10)
        assert len(splits) == 1

    def test_rejects_non_positive_sizes(self):
        bars = _bars(10)
        with pytest.raises(ValueError):
            rolling_splits(bars, train_size=0, test_size=5)
        with pytest.raises(ValueError):
            rolling_splits(bars, train_size=5, test_size=0)
        with pytest.raises(ValueError):
            rolling_splits(bars, train_size=5, test_size=5, step=0)


class TestSingleHoldoutSplit:
    def test_holds_out_the_most_recent_bars(self):
        bars = _bars(100)
        split = single_holdout_split(bars, test_size=20)
        assert len(split.train) == 80
        assert len(split.test) == 20
        assert split.test[0].ts == bars[80].ts
        assert split.train[-1].ts < split.test[0].ts

    def test_rejects_test_size_out_of_range(self):
        bars = _bars(10)
        with pytest.raises(ValueError):
            single_holdout_split(bars, test_size=0)
        with pytest.raises(ValueError):
            single_holdout_split(bars, test_size=10)
        with pytest.raises(ValueError):
            single_holdout_split(bars, test_size=11)
