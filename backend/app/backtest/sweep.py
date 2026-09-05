"""VectorBT walk-forward parameter sweep. BUILD_SPEC §8.5 rule 4.

This is the *fast* half of BUILD_SPEC's two-stage design: "Use VectorBT for
fast parameter sweeps, then re-verify the winner in [an event-driven] loop
to catch lookahead bugs the vectorized version hides." Per the task brief,
this stage does not need to call the literal `apply_friction` function or
reproduce every strategy rule exactly (that fidelity is what
`app.backtest.verify`/`app.backtest.runner` exist for) — its only job is to
pick a promising parameter set from in-sample data quickly, over a small
grid, without ever touching the out-of-sample window.

**Documented fidelity gap**: each strategy below is reduced to a vectorized
proxy of its real `app/strategies/*.py` logic — computed with
`pandas_ta_classic` directly over the whole in-sample series instead of
incrementally through `SymbolEngine` (which is what makes this fast: one
vectorized indicator pass over N bars instead of N incremental
recomputations). Concretely, every proxy below drops:
  - the cross-timeframe daily regime filter (ema_cross, vwap_reversion both
    gate entries on a *daily* EMA/SMA the strategy's own primary timeframe
    doesn't have bars for; approximating it correctly here would need a
    second, differently-indexed vectorized series joined back onto the
    primary one, which is exactly the kind of index-alignment bug this
    stage is explicitly allowed to risk since step 4 re-verifies for real);
  - ema_cross's ATR-based stop/trail and orb's/vwap_reversion's ATR-based
    stop (a fixed-bar exit or an opposite-signal exit stands in);
  - rsi2's 5-day hard time stop.
None of these gaps affect the *final* gate numbers — those come only from
`app.backtest.verify` running the actual `Strategy` subclass through
`app.backtest.runner`, friction and all, over the out-of-sample window.
This stage only ever influences which `params` dict gets handed to that
re-verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from typing import Protocol

import pandas as pd
import pandas_ta_classic as ta

from app.ingest.bars import FinalBar
from app.market_calendar import start_of_trading_day


class SignalFn(Protocol):
    def __call__(self, df: pd.DataFrame, params: dict) -> tuple[pd.Series, pd.Series]: ...


@dataclass(frozen=True)
class SweepCandidate:
    params: dict
    in_sample_return_pct: Decimal


@dataclass(frozen=True)
class SweepResult:
    best: SweepCandidate
    # Every combo tried, in the order evaluated -- BUILD_SPEC §8.5 rule 5:
    # "report the distribution of outcomes, not just the mean."
    all_candidates: list[SweepCandidate]


def _bars_to_frame(bars: list[FinalBar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        },
        index=pd.DatetimeIndex([b.ts for b in bars]),
    )


def _param_grid(grid: dict[str, list]) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, combo, strict=True)) for combo in product(*grid.values())]


# ---------------------------------------------------------------------------
# Per-strategy vectorized signal proxies -- see module docstring for gaps.
# ---------------------------------------------------------------------------


def _rsi2_signals(df: pd.DataFrame, params: dict) -> tuple[pd.Series, pd.Series]:
    """High-fidelity: rsi2 is already a daily-bar-only strategy with no
    cross-timeframe filter, so this proxy has no daily/primary mismatch to
    approximate away. Only the 5-day hard time stop is dropped (see module
    docstring) -- entries/exits otherwise match `Rsi2MeanReversion` exactly."""
    rsi2 = ta.rsi(df["close"], length=2)
    sma200 = ta.sma(df["close"], length=200)
    sma5 = ta.sma(df["close"], length=5)
    entries = (rsi2 < params["rsi_oversold_threshold"]) & (df["close"] > sma200)
    exits = (rsi2 > params["rsi_overbought_threshold"]) | (df["close"] > sma5)
    return entries.fillna(False), exits.fillna(False)


def _ema_cross_signals(df: pd.DataFrame, params: dict) -> tuple[pd.Series, pd.Series]:
    """Proxy: drops the daily SMA(200) regime filter (see module docstring)
    and the ATR stop/trail -- exit is simply the reverse crossunder."""
    fast = ta.ema(df["close"], length=9)
    slow = ta.ema(df["close"], length=21)
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return entries.fillna(False), exits.fillna(False)


def _session_vwap_and_std(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Vectorized session VWAP + volume-weighted std, grouped by trading day.

    Mirrors `app.strategies.indicators._session_vwap_and_bands`'s formulas,
    computed as a running (expanding-within-day) value at every bar rather
    than only at the session's current point -- the vectorized equivalent
    of what `SymbolEngine` recomputes incrementally bar by bar.
    """
    day = pd.Series([start_of_trading_day(ts) for ts in df.index], index=df.index)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum().replace(0, pd.NA)
    vwap = cum_pv / cum_vol
    sq_dev = (typical - vwap) ** 2 * df["volume"]
    variance = sq_dev.groupby(day).cumsum() / cum_vol
    std = variance.clip(lower=0) ** 0.5
    return vwap, std


def _vwap_reversion_signals(df: pd.DataFrame, params: dict) -> tuple[pd.Series, pd.Series]:
    """Proxy: drops the daily EMA(200) trend filter (see module docstring).
    Entry/exit otherwise match `VwapReversionStrategy`: entry k_std below the
    running session VWAP, exit on recovery to VWAP."""
    vwap, std = _session_vwap_and_std(df)
    threshold = vwap - params["k_std"] * std
    entries = df["close"] < threshold
    exits = df["close"] >= vwap
    return entries.fillna(False), exits.fillna(False)


def _opening_range(df: pd.DataFrame, minutes: int = 15) -> tuple[pd.Series, pd.Series]:
    """Per-session opening-range high/low, held constant for the rest of
    that trading day (vectorized equivalent of
    `app.strategies.indicators._opening_range`)."""
    day = pd.Series([start_of_trading_day(ts) for ts in df.index], index=df.index)
    session_start = day.groupby(day).transform("first")
    minutes_since_open = (df.index.to_series() - session_start).dt.total_seconds() / 60
    in_window = minutes_since_open < minutes
    high_in_window = df["high"].where(in_window)
    low_in_window = df["low"].where(in_window)
    or_high = high_in_window.groupby(day).cummax().groupby(day).transform("last")
    or_low = low_in_window.groupby(day).cummin().groupby(day).transform("last")
    # Forward-fill within the day so bars after the window keep the fixed
    # opening-range bound rather than NaN.
    or_high = or_high.groupby(day).ffill()
    or_low = or_low.groupby(day).ffill()
    return or_high, or_low


def _orb_signals(df: pd.DataFrame, params: dict) -> tuple[pd.Series, pd.Series]:
    """Proxy: relative-volume filter matches `OpeningRangeBreakout` (same
    rolling-20-bar-average definition); the ATR-based min-range-width filter
    and ATR stop are dropped (a fixed reverse-below-range exit stands in for
    the stop) -- see module docstring."""
    or_high, or_low = _opening_range(df, params.get("opening_range_minutes", 15))
    volume_avg_20 = df["volume"].rolling(20).mean()
    relative_volume = df["volume"] / volume_avg_20
    entries = (df["close"] > or_high) & (relative_volume > params["min_relative_volume"])
    exits = df["close"] < or_low
    return entries.fillna(False), exits.fillna(False)


_SIGNAL_FNS: dict[str, SignalFn] = {
    "rsi2": _rsi2_signals,
    "ema_cross": _ema_cross_signals,
    "vwap_reversion": _vwap_reversion_signals,
    "orb": _orb_signals,
}

_VBT_FREQ: dict[str, str] = {"1Day": "1D", "1Min": "1min", "5Min": "5min"}


def run_sweep(
    slug: str,
    bars: list[FinalBar],
    param_grid: dict[str, list],
    init_cash: Decimal = Decimal("100000"),
) -> SweepResult:
    """Run every combo in `param_grid` (a small, cartesian-product grid --
    "2-3 params per strategy" per the task brief) over `bars` (in-sample
    only) via VectorBT's vectorized `Portfolio.from_signals`, and return the
    combo with the highest in-sample total return alongside every combo's
    result (BUILD_SPEC §8.5 rule 5: report the distribution, not just the
    winner).

    Imports `vectorbt` lazily, on the first actual sweep call, rather than
    at module scope -- `vectorbt` (with its numba/llvmlite/plotly
    dependency chain) is a real, unremoved dependency of this feature, but
    its bundle size is too large for Vercel's serverless function to carry
    alongside the live-trading API (see pyproject.toml's `backtest` extras
    group). Importing it lazily means the rest of the app -- every route
    that isn't the backtest gate -- still boots and runs without it
    installed.
    """
    import vectorbt as vbt

    if slug not in _SIGNAL_FNS:
        raise ValueError(f"no vectorized sweep proxy for strategy slug {slug!r}")
    if not bars:
        raise ValueError("run_sweep requires at least one in-sample bar")

    df = _bars_to_frame(bars)
    signal_fn = _SIGNAL_FNS[slug]
    freq = _VBT_FREQ.get(bars[0].timeframe, "1min")

    candidates: list[SweepCandidate] = []
    for params in _param_grid(param_grid):
        entries, exits = signal_fn(df, params)
        portfolio = vbt.Portfolio.from_signals(
            df["close"], entries, exits,
            init_cash=float(init_cash), fees=0.0, freq=freq,
        )
        total_return = Decimal(str(portfolio.total_return()))
        candidates.append(SweepCandidate(params=params, in_sample_return_pct=total_return))

    best = max(candidates, key=lambda c: c.in_sample_return_pct)
    return SweepResult(best=best, all_candidates=candidates)
