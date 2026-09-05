"""The friction-consistent, event-driven backtest runner. BUILD_SPEC §8.5.

This is the one place a strategy's historical performance is actually
simulated. It is deliberately built by *reusing* the exact same pieces live
trading uses, rather than reimplementing a parallel "backtest version" of
any of them:

- `app.strategies.engine.SymbolEngine` / `evaluate_strategies` — the same
  bar-finalization gating and indicator pipeline live trading uses. A
  strategy fed through this runner cannot see a bar out of order or ahead
  of when it would really be known, because `SymbolEngine` itself refuses
  out-of-order bars (CLAUDE.md rule 4, BUILD_SPEC §8.5 rule 1).
- `app.execution.friction.apply_friction` — the literal function, not a
  reimplementation, for every simulated fill (CLAUDE.md rule 3, BUILD_SPEC
  §8.5 rule 2: "friction applied identically to live paper. Same code
  path.").
- `app.execution.positions.close_position` — the same P&L/R-multiple math
  live trading uses to turn a closed round trip into a `ClosedTrade`.
- `app.risk.sizing.fixed_fractional_qty`/`clamp_to_max_position` — the same
  pure position-sizing functions the live risk engine uses (see
  `app.execution.order_service.submit_manual_order`), so backtested trades
  are sized the same way a live trade would be.

Execution timing (BUILD_SPEC §8.5 rule 1 / CLAUDE.md rule 4): a strategy
evaluated on finalized bar `t` can only ever act on the *next* bar, `t+1` —
its signal is queued as `pending_signal` and filled at `t+1`'s open, never
at `t`'s own close. This is the direct mechanical implementation of "no
lookahead, ever."

Friction inputs from historical bars (no bid/ask in Alpaca's historical bar
data, documented per the task brief):

- **bid/ask proxy**: centered on the fill bar's `open` (since that's the
  actual reference/execution price BUILD_SPEC's timing rule fills at), with
  the *width* of the synthetic spread set to half the fill bar's own
  high-low range. This keeps `apply_friction`'s `mid_price(bid, ask)` equal
  to the bar's open (the true execution reference) while still deriving a
  spread magnitude from real observed intrabar volatility rather than an
  arbitrary constant.
- **atr**: the decision-time `ctx.indicators.atr_14` from the bar the
  signal actually fired on (bar `t`, not `t+1`) — the same volatility
  reading the strategy itself used to size its stop, and never a
  lookahead value. Falls back to `Decimal(0)` (no slippage-widening term,
  spread cost still applies) on the rare bar where ATR hasn't warmed up.
- **typical_bar_volume**: a rolling mean of the last `volume_lookback` bars'
  volume, computed from bars strictly *before* the fill bar — deliberately
  excluding the fill bar's own volume, matching `FrictionInput`'s own
  docstring ("not the same as the bar's own volume").

Position sizing mirrors `order_service.submit_manual_order`: qty is sized
against the *reference* price (here, the synthetic quote's mid == the fill
bar's open) and stop distance, then clamped to `max_position_pct` of
running equity — the same equity the fill is priced against, so sizing and
filling are never inconsistent with each other.

Any position still open when the historical data runs out is left
unrealized and is **not** force-closed with a fabricated exit price — only
genuinely completed round trips are returned as trades (CLAUDE.md's
anti-fabrication principle applies here just as much as to the gate).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.execution.friction import FrictionConfig, FrictionInput, apply_friction
from app.execution.positions import ClosedTrade, OpenPosition, close_position
from app.ingest.bars import FinalBar
from app.market_calendar import NY
from app.risk.sizing import clamp_to_max_position, fixed_fractional_qty
from app.strategies.base import Signal, Strategy
from app.strategies.engine import SymbolEngine, evaluate_strategies

# Mirrors app.execution.order_service.DEFAULT_RISK_SETTINGS (risk_settings
# table's own column defaults, BUILD_SPEC §5) — duplicated here rather than
# imported so app/backtest stays decoupled from app/execution/order_service
# (an HTTP-orchestration module, not a reusable primitive).
_DEFAULT_RISK_PER_TRADE_PCT = Decimal("0.01")
_DEFAULT_MAX_POSITION_PCT = Decimal("0.20")


@dataclass(frozen=True)
class BacktestConfig:
    starting_equity: Decimal = Decimal("100000")
    risk_per_trade_pct: Decimal = _DEFAULT_RISK_PER_TRADE_PCT
    max_position_pct: Decimal = _DEFAULT_MAX_POSITION_PCT
    # Rolling window (in bars) for the typical_bar_volume friction input.
    volume_lookback: int = 20
    friction_config: FrictionConfig = field(default_factory=FrictionConfig)


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    strategy_slug: str
    trades: list[ClosedTrade]
    # Account equity (starting_equity + cumulative realized net_pnl) at each
    # trade close, in trade order. Purely informational -- gate.py computes
    # its drawdown/expectancy stats directly off `trades`, not this.
    equity_curve: list[tuple[object, Decimal]]
    ended_with_open_position: bool


def _synthetic_quote(bar: FinalBar) -> tuple[Decimal, Decimal]:
    """Bid/ask proxy for a historical fill -- see module docstring."""
    half_range = (bar.high - bar.low) / Decimal(2)
    quarter_range = half_range / Decimal(2)
    return bar.open - quarter_range, bar.open + quarter_range


def _typical_volume(window: deque[int]) -> Decimal:
    if not window:
        return Decimal(0)
    return Decimal(sum(window)) / Decimal(len(window))


@dataclass
class _PendingSignal:
    signal: Signal
    atr: Decimal


def run_backtest(
    strategy: Strategy,
    symbol: str,
    primary_bars: list[FinalBar],
    daily_bars: list[FinalBar] | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Simulate `strategy` bar-by-bar over `primary_bars` (in `strategy.timeframe`).

    `daily_bars` supplies the daily regime-filter history (`ctx.daily_history`/
    `ctx.daily_indicators`) for strategies whose primary timeframe isn't
    already daily (ema_cross, vwap_reversion) -- see `BarContext`'s own
    docstring. Ignored (and unnecessary) when `strategy.timeframe == "1Day"`.
    """
    config = config or BacktestConfig()
    engine = SymbolEngine(symbol)

    daily_by_date: dict[date, FinalBar] = {}
    if strategy.timeframe != "1Day" and daily_bars:
        daily_by_date = {b.ts.astimezone(NY).date(): b for b in daily_bars}
    fed_daily_dates: set[date] = set()

    equity = config.starting_equity
    open_position: OpenPosition | None = None
    open_position_entry_friction = Decimal(0)
    pending: _PendingSignal | None = None

    trades: list[ClosedTrade] = []
    equity_curve: list[tuple[object, Decimal]] = []
    volume_window: deque[int] = deque(maxlen=config.volume_lookback)

    for bar in primary_bars:
        # Feed any daily bars for trading days strictly before this bar's
        # day -- a day's own daily bar is only known once that day has
        # closed, so it must never be fed before that day's intraday bars
        # have all been processed (BUILD_SPEC §8.5 rule 1: no lookahead).
        if daily_by_date:
            bar_day = bar.ts.astimezone(NY).date()
            for d in sorted(d for d in daily_by_date if d < bar_day and d not in fed_daily_dates):
                engine.on_finalized_bar(daily_by_date[d])
                fed_daily_dates.add(d)

        # 1) Fill any signal from the PREVIOUS bar at THIS bar's open.
        if pending is not None:
            typical_volume = _typical_volume(volume_window)
            bid, ask = _synthetic_quote(bar)
            # mid_price(bid, ask) == bar.open by construction (see
            # _synthetic_quote) -- this is the reference/execution price
            # BUILD_SPEC's "fill at t+1's open" rule calls for, used for
            # both position sizing and as apply_friction's mid.
            reference_price = bar.open

            if pending.signal.intent == "entry" and open_position is None:
                stop_price = pending.signal.stop_price
                if stop_price is not None:
                    qty = fixed_fractional_qty(
                        equity, config.risk_per_trade_pct, reference_price, stop_price
                    )
                    qty = clamp_to_max_position(
                        qty, reference_price, equity, config.max_position_pct
                    )
                    if qty > 0:
                        fill = apply_friction(
                            FrictionInput(
                                side=pending.signal.side, qty=qty, ts=bar.ts,
                                bid=bid, ask=ask, atr=pending.atr,
                                typical_bar_volume=typical_volume,
                            ),
                            config.friction_config,
                        )
                        open_position = OpenPosition(
                            symbol=symbol,
                            side=pending.signal.side,
                            qty=qty,
                            avg_entry_price=fill.fill_price,
                            stop_price=stop_price,
                            opened_at=bar.ts,
                        )
                        open_position_entry_friction = fill.total_friction

            elif pending.signal.intent == "exit" and open_position is not None:
                qty = open_position.qty
                fill = apply_friction(
                    FrictionInput(
                        side=pending.signal.side, qty=qty, ts=bar.ts,
                        bid=bid, ask=ask, atr=pending.atr,
                        typical_bar_volume=typical_volume,
                    ),
                    config.friction_config,
                )
                closed = close_position(
                    open_position,
                    exit_price=fill.fill_price,
                    exit_qty=qty,
                    entry_friction=open_position_entry_friction,
                    exit_friction=fill.total_friction,
                    closed_at=bar.ts,
                    # This runner only ever exits on a strategy-emitted
                    # Signal (manage()'s stop/target/time-stop logic all
                    # surface as an exit Signal, per app/strategies/*.py) --
                    # 'signal' is the correct ExitReason bucket, not the
                    # finer-grained rule_id the strategy attached.
                    exit_reason="signal",
                )
                trades.append(closed)
                equity += closed.net_pnl
                equity_curve.append((closed.closed_at, equity))
                open_position = None
                open_position_entry_friction = Decimal(0)

            pending = None

        # 2) Feed this bar and let the strategy react to it -- queued for
        #    the *next* bar's open, never acted on immediately.
        ctx = engine.on_finalized_bar(bar)
        signals = evaluate_strategies([strategy], ctx, open_position)
        if signals:
            atr = ctx.indicators.atr_14
            pending = _PendingSignal(
                signal=signals[0],
                atr=Decimal(str(atr)) if atr is not None else Decimal(0),
            )

        volume_window.append(bar.volume)

    return BacktestResult(
        symbol=symbol,
        strategy_slug=strategy.slug,
        trades=trades,
        equity_curve=equity_curve,
        ended_with_open_position=open_position is not None,
    )
