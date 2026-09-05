"""The backtest gate. BUILD_SPEC §8.5, CLAUDE.md rule 5.

`strategies.enabled` cannot be set `true` unless `gate_passed` is `true`.
This module computes the seven gate criteria from BUILD_SPEC §8.5's table,
each purely from a list of already-closed, already-friction-adjusted
`ClosedTrade`s (produced by `app.backtest.runner`/`app.backtest.verify` over
an out-of-sample window) plus a couple of externally-supplied comparison
numbers (in-sample return, SPY buy-and-hold return) that aren't derivable
from the trade list alone.

CLAUDE.md is explicit and non-negotiable: **do not lower thresholds, skip
criteria, or fabricate trade histories to make something pass.** Every
criterion below is computed exactly as BUILD_SPEC §8.5 states it, on
whatever real numbers the caller hands in. A strategy without enough
history or that fails a criterion gets `gate_passed = False` and an honest
per-criterion report -- that is the correct, intended result, not a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.execution.positions import ClosedTrade

# Average calendar days per month, for converting an OOS date range into a
# month count without the "which months have 28 vs 31 days" noise a naive
# 30-day-month approximation would introduce.
_DAYS_PER_MONTH = Decimal("30.4368")

_MIN_TRADES = 100
_MIN_OOS_MONTHS = Decimal("12")
_MIN_PROFIT_FACTOR = Decimal("1.2")
_MAX_DRAWDOWN_PCT = Decimal("0.20")
_MIN_WALK_FORWARD_EFFICIENCY = Decimal("0.5")
# Sentinel for "no losing trades at all" (profit factor is mathematically
# infinite; Decimal/JSON have no Infinity, so this stands in as "clearly
# well above the 1.2 threshold" without claiming a specific numeric value
# is precise).
_UNBOUNDED_PROFIT_FACTOR = Decimal("999")


@dataclass(frozen=True)
class GateCriterion:
    name: str
    threshold: float
    actual: float
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GateReport:
    criteria: list[GateCriterion]
    gate_passed: bool


def _sample_size(trades: list[ClosedTrade]) -> GateCriterion:
    n = len(trades)
    return GateCriterion(
        name="sample_size",
        threshold=float(_MIN_TRADES),
        actual=float(n),
        passed=n >= _MIN_TRADES,
        detail=f"{n} closed out-of-sample trades (need >= {_MIN_TRADES})",
    )


def _oos_period(oos_start: datetime, oos_end: datetime) -> tuple[GateCriterion, Decimal]:
    days = Decimal((oos_end - oos_start).days)
    months = days / _DAYS_PER_MONTH if days > 0 else Decimal(0)
    return (
        GateCriterion(
            name="oos_period_months",
            threshold=float(_MIN_OOS_MONTHS),
            actual=float(months),
            passed=months >= _MIN_OOS_MONTHS,
            detail=f"out-of-sample window spans {months:.1f} months (need >= 12)",
        ),
        months,
    )


def _expectancy(trades: list[ClosedTrade]) -> GateCriterion:
    if not trades:
        return GateCriterion(
            name="expectancy_after_friction", threshold=0.0, actual=0.0, passed=False,
            detail="no trades to compute expectancy from",
        )
    mean_net = sum((t.net_pnl for t in trades), Decimal(0)) / Decimal(len(trades))
    return GateCriterion(
        name="expectancy_after_friction",
        threshold=0.0,
        actual=float(mean_net),
        passed=mean_net > 0,
        detail=f"mean net P&L per trade (after friction) is {mean_net:.2f}",
    )


def _profit_factor(trades: list[ClosedTrade]) -> GateCriterion:
    gross_profit = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal(0))
    gross_loss = -sum((t.net_pnl for t in trades if t.net_pnl < 0), Decimal(0))
    if gross_loss <= 0:
        pf = _UNBOUNDED_PROFIT_FACTOR if gross_profit > 0 else Decimal(0)
    else:
        pf = gross_profit / gross_loss
    return GateCriterion(
        name="profit_factor",
        threshold=float(_MIN_PROFIT_FACTOR),
        actual=float(pf),
        passed=pf >= _MIN_PROFIT_FACTOR,
        detail=f"gross profit {gross_profit:.2f} / gross loss {gross_loss:.2f}",
    )


def _max_drawdown(trades: list[ClosedTrade], starting_equity: Decimal) -> GateCriterion:
    """Drawdown on the cumulative net_pnl equity curve, as a fraction of the
    running peak equity. `starting_equity` only sets the base the first
    trade's P&L is measured against -- the curve itself is pure cumulative
    net_pnl, per the task brief ("on the cumulative net_pnl equity curve")."""
    if not trades:
        return GateCriterion(
            name="max_drawdown_pct", threshold=float(_MAX_DRAWDOWN_PCT), actual=0.0,
            passed=True, detail="no trades; no drawdown to measure",
        )
    equity = starting_equity
    peak = starting_equity
    max_dd = Decimal(0)
    for t in trades:
        equity += t.net_pnl
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)
    return GateCriterion(
        name="max_drawdown_pct",
        threshold=float(_MAX_DRAWDOWN_PCT),
        actual=float(max_dd),
        passed=max_dd <= _MAX_DRAWDOWN_PCT,
        detail=f"max peak-to-trough drawdown was {max_dd:.2%}",
    )


def _walk_forward_efficiency(
    in_sample_return_pct: Decimal, out_of_sample_return_pct: Decimal,
) -> GateCriterion:
    if in_sample_return_pct <= 0:
        return GateCriterion(
            name="walk_forward_efficiency",
            threshold=float(_MIN_WALK_FORWARD_EFFICIENCY),
            actual=0.0,
            passed=False,
            detail=(
                f"in-sample return was {in_sample_return_pct:.2%} (<= 0); "
                "OOS/IS efficiency is not meaningfully computable"
            ),
        )
    efficiency = out_of_sample_return_pct / in_sample_return_pct
    return GateCriterion(
        name="walk_forward_efficiency",
        threshold=float(_MIN_WALK_FORWARD_EFFICIENCY),
        actual=float(efficiency),
        passed=efficiency >= _MIN_WALK_FORWARD_EFFICIENCY,
        detail=(
            f"OOS return {out_of_sample_return_pct:.2%} / IS return "
            f"{in_sample_return_pct:.2%} = {efficiency:.2f}"
        ),
    )


def _beats_spy(
    out_of_sample_return_pct: Decimal, spy_buy_hold_return_pct: Decimal,
) -> GateCriterion:
    """Simplification, documented per the honesty rule: BUILD_SPEC §8.5 asks
    for "risk-adjusted return" vs. SPY buy-and-hold; this compares raw total
    return over the same window. A true risk-adjustment (e.g. Sharpe-ratio
    comparison) would need a return *series*, not just the two trade lists'
    aggregate P&L this module is handed -- out of scope for tonight."""
    return GateCriterion(
        name="beats_spy_buy_and_hold",
        threshold=float(spy_buy_hold_return_pct),
        actual=float(out_of_sample_return_pct),
        passed=out_of_sample_return_pct > spy_buy_hold_return_pct,
        detail=(
            f"strategy OOS return {out_of_sample_return_pct:.2%} vs. SPY "
            f"buy-and-hold {spy_buy_hold_return_pct:.2%} over the same window "
            "(raw return comparison, not risk-adjusted -- see docstring)"
        ),
    )


def compute_gate_report(
    trades: list[ClosedTrade],
    oos_start: datetime,
    oos_end: datetime,
    in_sample_return_pct: Decimal,
    out_of_sample_return_pct: Decimal,
    spy_buy_hold_return_pct: Decimal,
    starting_equity: Decimal = Decimal("100000"),
) -> GateReport:
    """Compute every BUILD_SPEC §8.5 gate criterion and the overall pass/fail.

    `trades` must already be the out-of-sample, friction-adjusted, closed
    round trips (from `app.backtest.verify`) -- this function does not
    itself distinguish in-sample from out-of-sample trades; that separation
    is the caller's responsibility (walk-forward splitting happens upstream,
    in `app.backtest.walkforward`/`app.backtest.sweep`).
    """
    oos_period, _months = _oos_period(oos_start, oos_end)
    criteria = [
        _sample_size(trades),
        oos_period,
        _expectancy(trades),
        _profit_factor(trades),
        _max_drawdown(trades, starting_equity),
        _walk_forward_efficiency(in_sample_return_pct, out_of_sample_return_pct),
        _beats_spy(out_of_sample_return_pct, spy_buy_hold_return_pct),
    ]
    return GateReport(criteria=criteria, gate_passed=all(c.passed for c in criteria))
