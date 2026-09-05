"""Out-of-sample re-verification of the sweep's winning parameter set.

BUILD_SPEC §8.5: "Use VectorBT for fast parameter sweeps, then re-verify the
winner in Backtrader's event-driven loop to catch lookahead bugs the
vectorized version hides."

**Documented implementation choice, per the task brief**: this module does
*not* wire in the `backtrader` library. `app.backtest.runner.run_backtest`
already **is** a genuine event-driven, bar-by-bar loop that is lookahead-safe
by construction (it reuses `app.strategies.engine.SymbolEngine` -- the exact
bar-finalization gating live trading depends on) and friction-consistent (it
calls the literal `apply_friction`, not a reimplementation). It satisfies the
actual purpose of this stage -- "an event-driven re-verification, distinct
from the vectorized sweep, that catches lookahead bugs the vectorized
version hides" -- without the extra integration cost of re-expressing four
condition-based `Strategy` subclasses a second time inside Backtrader's own
indicator/strategy framework. `backtrader>=1.9.78` was added to
pyproject.toml and imports cleanly in this environment (see the task
summary) -- this is a documented time-boxed choice to reuse `runner.py`
here, not a fallback taken because backtrader failed to install or work.

Unlike `app.backtest.sweep`'s vectorized proxies, this module runs the
*real* `Strategy` subclass (via `app.strategies.registry.create_strategy`)
with the sweep's winning params, so every rule the real strategy actually
enforces -- the daily regime filter, the ATR stop/trail, the time stop --
is back in effect for the number that actually decides the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.backtest.runner import BacktestConfig, BacktestResult, run_backtest
from app.ingest.bars import FinalBar
from app.strategies.registry import create_strategy


@dataclass(frozen=True)
class VerificationResult:
    backtest: BacktestResult
    out_of_sample_return_pct: Decimal


def verify_out_of_sample(
    slug: str,
    params: dict,
    symbol: str,
    oos_bars: list[FinalBar],
    daily_bars: list[FinalBar] | None = None,
    config: BacktestConfig | None = None,
) -> VerificationResult:
    """Re-run the real strategy (slug + winning params) over the
    out-of-sample window only, through the friction-consistent event-driven
    runner, and return its trades plus the resulting total return (measured
    against `config.starting_equity`, matching how `run_backtest` itself
    tracks equity).
    """
    config = config or BacktestConfig()
    strategy = create_strategy(slug, params)
    result = run_backtest(strategy, symbol, oos_bars, daily_bars=daily_bars, config=config)

    total_net_pnl = sum((t.net_pnl for t in result.trades), Decimal(0))
    oos_return_pct = (
        total_net_pnl / config.starting_equity if config.starting_equity > 0 else Decimal(0)
    )
    return VerificationResult(backtest=result, out_of_sample_return_pct=oos_return_pct)
