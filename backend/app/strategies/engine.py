"""The strategy engine: bar-finalization gating. BUILD_SPEC §7.2, §7.3.

Strategies evaluate only on finalized bars — evaluating a forming bar is a
lookahead bug that makes backtests look brilliant and live results
terrible. This module is the one place that guarantee actually lives: it
only ever accepts `FinalBar` (never `WorkingBar`, which `BarBuilder`
structurally never hands out — see app/ingest/bars.py), and every
`BarContext` it builds is constructed purely from bars already appended to
its own rolling history, so nothing here can ever see a bar from the
future relative to the one just finalized.
"""

from __future__ import annotations

from app.execution.positions import OpenPosition
from app.ingest.bars import FinalBar
from app.strategies.base import BarContext, Signal, Strategy
from app.strategies.indicators import compute_indicators

# Bounded rolling window per symbol/timeframe: enough for the longest
# indicator (EMA/SMA 200) plus headroom, without unbounded memory growth.
_MAX_HISTORY = 250


class SymbolEngine:
    """Bar-finalization gating and indicator computation for one symbol.

    Owns the rolling bar history per timeframe. Feed it `FinalBar`s in
    arrival order via `on_finalized_bar` — never a bar still being built.
    """

    def __init__(self, symbol: str, max_history: int = _MAX_HISTORY):
        self.symbol = symbol
        self.max_history = max_history
        self._history: dict[str, list[FinalBar]] = {}  # timeframe -> bars, oldest first

    def history_for(self, timeframe: str) -> list[FinalBar]:
        return list(self._history.get(timeframe, ()))

    def on_finalized_bar(self, bar: FinalBar) -> BarContext:
        """Append a finalized bar and return the BarContext as of it.

        Raises on an out-of-order/duplicate bar (ts <= the last one already
        recorded for this timeframe) rather than silently accepting it —
        that would mean something upstream let a stale or re-ordered bar
        through, which is exactly the kind of bug that corrupts every
        indicator computed from it (BUILD_SPEC §7.1).
        """
        if bar.symbol != self.symbol:
            raise ValueError(f"bar for {bar.symbol} given to engine for {self.symbol}")

        history = self._history.setdefault(bar.timeframe, [])
        if history and bar.ts <= history[-1].ts:
            raise ValueError(
                f"out-of-order bar for {self.symbol}/{bar.timeframe}: "
                f"{bar.ts} is not after last recorded {history[-1].ts}"
            )
        history.append(bar)
        if len(history) > self.max_history:
            del history[: len(history) - self.max_history]

        indicators = compute_indicators(history)

        daily_history = None
        daily_indicators = None
        if bar.timeframe != "1Day":
            daily_bars = self._history.get("1Day")
            if daily_bars:
                daily_history = list(daily_bars)
                daily_indicators = compute_indicators(daily_history)

        return BarContext(
            symbol=self.symbol,
            bar=bar,
            history=list(history),
            indicators=indicators,
            daily_history=daily_history,
            daily_indicators=daily_indicators,
        )


def evaluate_strategies(
    strategies: list[Strategy],
    ctx: BarContext,
    open_position: OpenPosition | None,
) -> list[Signal]:
    """Run every strategy whose timeframe matches `ctx.bar` against it.

    With an open position, only `manage()` runs (an exit, or nothing) —
    `evaluate()` never fires a new entry while one is already open for that
    symbol. The caller supplies the right BarContext per timeframe (see
    `SymbolEngine.history_for`); a strategy on a different timeframe than
    `ctx.bar` is simply skipped for this call.
    """
    signals: list[Signal] = []
    for strategy in strategies:
        if strategy.timeframe != ctx.bar.timeframe:
            continue
        signal = (
            strategy.manage(ctx, open_position)
            if open_position is not None
            else strategy.evaluate(ctx)
        )
        if signal is not None:
            signals.append(signal)
    return signals
