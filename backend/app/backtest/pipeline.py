"""End-to-end backtest pipeline glue: split -> sweep -> verify -> gate.

Kept DB-free and pure (bars in, `GateReport` out) like
`app.execution.order_service`'s internal helpers, so `POST
/strategies/{id}/backtest` (`app/api/routes_strategies.py`) can call it
directly and tests can exercise it with small synthetic bar lists instead of
real multi-year Alpaca data (fetching, sweeping, and re-verifying over a
real 12-month 1-minute OOS window takes minutes, not milliseconds -- fine
for an interactive/manual run, wrong for a test suite that needs to run in
seconds; see the task summary for real measured timings).

This module owns exactly one policy decision that isn't specified
elsewhere: how big the out-of-sample holdout window is per timeframe, and
what the default sweep param grid is per strategy. Both are small,
documented, and overridable by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.backtest.gate import GateReport, compute_gate_report
from app.backtest.runner import BacktestConfig
from app.backtest.sweep import run_sweep
from app.backtest.verify import verify_out_of_sample
from app.backtest.walkforward import single_holdout_split
from app.ingest.bars import FinalBar

# Small, documented per-strategy sweep grids ("2-3 params per strategy,
# small grids given time constraints" -- task brief). ema_cross's
# vectorized sweep proxy (app.backtest.sweep._ema_cross_signals) has no
# tunable parameters -- its EMA(9)/EMA(21) lengths are fixed in the proxy --
# so its grid is empty, which app.backtest.sweep.run_sweep treats as "one
# candidate, the empty params dict" (the real strategy then falls back to
# its own class defaults during out-of-sample re-verification).
DEFAULT_PARAM_GRIDS: dict[str, dict[str, list]] = {
    "orb": {
        "min_relative_volume": [1.0, 1.2, 1.5],
        "opening_range_minutes": [15, 30],
    },
    "vwap_reversion": {"k_std": [1.5, 2.0, 2.5]},
    "ema_cross": {},
    "rsi2": {
        "rsi_oversold_threshold": [5.0, 10.0, 15.0],
        "rsi_overbought_threshold": [65.0, 70.0, 75.0],
    },
}

# Approximate bars per 12-month out-of-sample window, per timeframe (regular
# session: 6.5h = 390 one-minute bars/day, 78 five-minute bars/day, ~252
# trading days/year). Used only to size the walk-forward holdout split --
# not a claim about calendar days, which `app.backtest.gate` computes
# separately and exactly from the OOS bars' own first/last timestamps. Bumped
# a few percent past the bare 252/78*252/390*252 trading-day counts: 252
# consecutive trading days spans only ~364 *calendar* days (holidays/long
# weekends push it a hair under 12 months), which made this miss its own
# oos_period_months >= 12 gate criterion by a rounding error in practice --
# the extra bars are pure safety margin against that, not a different
# definition of "12 months".
_OOS_BAR_COUNT: dict[str, int] = {
    "1Day": 262,
    "5Min": 78 * 262,
    "1Min": 390 * 262,
}


@dataclass(frozen=True)
class BacktestPipelineResult:
    gate_report: GateReport
    winning_params: dict
    in_sample_bar_count: int
    out_of_sample_bar_count: int
    out_of_sample_trade_count: int


def _buy_and_hold_return_pct(
    spy_daily_bars: list[FinalBar], window_start: datetime, window_end: datetime
) -> Decimal:
    in_window = [b for b in spy_daily_bars if window_start <= b.ts <= window_end]
    if len(in_window) < 2 or in_window[0].close <= 0:
        return Decimal(0)
    return (in_window[-1].close - in_window[0].close) / in_window[0].close


def run_full_backtest(
    slug: str,
    params: dict,
    timeframe: str,
    symbol: str,
    primary_bars: list[FinalBar],
    spy_daily_bars: list[FinalBar],
    daily_bars: list[FinalBar] | None = None,
    param_grid: dict[str, list] | None = None,
    oos_bar_count: int | None = None,
    config: BacktestConfig | None = None,
) -> BacktestPipelineResult:
    """Split `primary_bars` into in-sample/out-of-sample, sweep the
    in-sample slice for a winning param set, re-verify it out-of-sample, and
    compute the BUILD_SPEC §8.5 gate report.

    `daily_bars` is the supplementary daily history for strategies whose
    primary timeframe isn't already daily (passed straight through to
    `app.backtest.runner.run_backtest` -- see its docstring). `spy_daily_bars`
    is SPY's own daily history, independent of `symbol`/`timeframe`, used
    only for the gate's "beats SPY buy-and-hold" criterion.

    When there isn't enough history for a full out-of-sample window (a new
    strategy, or a symbol/timeframe this session only fetched a short
    window for), everything available is treated as the out-of-sample set
    with an empty in-sample set -- no sweep runs, the strategy's own/passed
    params are used as-is, and the gate criteria that need real history
    (sample size, OOS period, walk-forward efficiency) correctly fail. This
    is the intended, honest behavior (CLAUDE.md), not an error case.
    """
    config = config or BacktestConfig()
    grid = param_grid if param_grid is not None else DEFAULT_PARAM_GRIDS.get(slug, {})
    bar_count = oos_bar_count or _OOS_BAR_COUNT.get(timeframe, 252)

    if not primary_bars:
        # No history at all -- every gate criterion that depends on real
        # bars correctly fails; there is nothing to sweep or verify.
        now = datetime.now(UTC)
        report = compute_gate_report(
            trades=[], oos_start=now, oos_end=now,
            in_sample_return_pct=Decimal(0), out_of_sample_return_pct=Decimal(0),
            spy_buy_hold_return_pct=Decimal(0), starting_equity=config.starting_equity,
        )
        return BacktestPipelineResult(
            gate_report=report, winning_params=params,
            in_sample_bar_count=0, out_of_sample_bar_count=0, out_of_sample_trade_count=0,
        )

    if len(primary_bars) > bar_count:
        split = single_holdout_split(primary_bars, bar_count)
        train_bars, test_bars = split.train, split.test
    else:
        train_bars, test_bars = [], primary_bars

    if train_bars:
        sweep_result = run_sweep(slug, train_bars, grid)
        winning_params = sweep_result.best.params
        in_sample_return_pct = sweep_result.best.in_sample_return_pct
    else:
        winning_params = params
        in_sample_return_pct = Decimal(0)

    verification = verify_out_of_sample(
        slug, winning_params, symbol, test_bars, daily_bars=daily_bars, config=config,
    )

    oos_start = test_bars[0].ts
    oos_end = test_bars[-1].ts
    spy_return_pct = _buy_and_hold_return_pct(spy_daily_bars, oos_start, oos_end)

    report = compute_gate_report(
        trades=verification.backtest.trades,
        oos_start=oos_start,
        oos_end=oos_end,
        in_sample_return_pct=in_sample_return_pct,
        out_of_sample_return_pct=verification.out_of_sample_return_pct,
        spy_buy_hold_return_pct=spy_return_pct,
        starting_equity=config.starting_equity,
    )
    return BacktestPipelineResult(
        gate_report=report,
        winning_params=winning_params,
        in_sample_bar_count=len(train_bars),
        out_of_sample_bar_count=len(test_bars),
        out_of_sample_trade_count=len(verification.backtest.trades),
    )
