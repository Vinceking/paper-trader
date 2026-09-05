"""VectorBT parameter sweep. BUILD_SPEC §8.5 rule 4 ("parameter sweeps must
be walk-forward, not a single global optimization") -- these tests check the
sweep mechanics (every grid combo evaluated, the best one selected, the
distribution reported) on small synthetic bar series, not real market data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.backtest.sweep import run_sweep
from app.ingest.bars import FinalBar


def _daily_bars(closes: list[float]) -> list[FinalBar]:
    start = datetime(2022, 1, 3, tzinfo=UTC)  # a Monday
    bars = []
    for i, c in enumerate(closes):
        close = Decimal(str(c))
        bars.append(
            FinalBar(
                symbol="SPY", timeframe="1Day", ts=start + timedelta(days=i),
                open=close, high=close + Decimal("1"), low=close - Decimal("1"),
                close=close, volume=1_000_000, vwap=None, trade_count=1000,
            )
        )
    return bars


class TestRunSweepGridCoverage:
    def test_every_grid_combo_is_evaluated(self):
        # 250 bars of pure uptrend -- enough for rsi2's SMA(200) to warm up.
        closes = [100.0 + 0.1 * i for i in range(250)]
        bars = _daily_bars(closes)

        grid = {
            "rsi_oversold_threshold": [10.0, 20.0],
            "rsi_overbought_threshold": [70.0, 80.0],
        }
        result = run_sweep("rsi2", bars, grid)

        assert len(result.all_candidates) == 4  # 2 x 2 cartesian product
        seen = {tuple(sorted(c.params.items())) for c in result.all_candidates}
        assert len(seen) == 4  # every combo distinct

    def test_best_is_the_max_in_sample_return_candidate(self):
        closes = [100.0 + 0.1 * i for i in range(250)]
        bars = _daily_bars(closes)
        grid = {
            "rsi_oversold_threshold": [10.0, 20.0],
            "rsi_overbought_threshold": [70.0, 80.0],
        }
        result = run_sweep("rsi2", bars, grid)
        assert result.best.in_sample_return_pct == max(
            c.in_sample_return_pct for c in result.all_candidates
        )
        assert result.best in result.all_candidates

    def test_unknown_strategy_slug_raises(self):
        bars = _daily_bars([100.0] * 10)
        try:
            run_sweep("not_a_real_strategy", bars, {})
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_empty_grid_produces_one_candidate_with_empty_params(self):
        closes = [100.0 + 0.05 * i for i in range(250)]
        bars = _daily_bars(closes)
        result = run_sweep("ema_cross", bars, {})
        assert len(result.all_candidates) == 1
        assert result.all_candidates[0].params == {}
