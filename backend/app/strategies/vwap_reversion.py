"""VWAP Mean Reversion strategy. BUILD_SPEC §8.3 item 2.

Long-only (this codebase never shorts — BUILD_SPEC §0.3, CLAUDE.md). Entry
fires when price has fallen more than `k_std` standard deviations below the
current session VWAP *and* the daily trend filter is up (price above the
200-period EMA). BUILD_SPEC is explicit that the trend filter is not
optional: mean-reverting against a downtrend is how people learn about
falling knives the expensive way.

BUILD_SPEC's own wording is "the **daily** trend filter" — same phrasing
style as EMA crossover's "the daily close is above the 200-day SMA" (§8.3
item 3), which is unambiguously daily bars. So this filter reads from
`ctx.daily_indicators.ema_200` (an actual 200-*day* EMA), not
`ctx.indicators.ema_200` (which on this strategy's own 1Min timeframe would
be a 200-*minute* EMA — a very different, much shorter-horizon filter).
Exactly like EMA crossover, this is `None` until daily bars have been fed to
the engine for this symbol; treated as "condition fails," not an error.

Both conditions are computed unconditionally, every call, via
`_entry_conditions` — never short-circuited — so the `signals` row always
carries the complete picture (CLAUDE.md rule 2 / BUILD_SPEC §8.2), including
which condition failed when the strategy declines to enter.

Exit (`manage`) fires on either:
  - a VWAP touch: the current bar's close has recovered to or above the
    *current* session VWAP (`ctx.indicators.vwap`) — deliberately NOT the
    VWAP value recorded on the position at entry time, because VWAP is a
    running volume-weighted average that drifts forward through the
    session. Comparing against a stale entry-time VWAP would make the exit
    check meaningless a few bars in.
  - the ATR stop: the current bar's close has dropped to or below
    `position.stop_price`, which was fixed at entry as
    `entry_price - atr_stop_mult * ATR(14)`. This is intentionally NOT
    recomputed from the *current* ATR — the stop distance is a decision
    made once, at entry, from the volatility observed at that instant; a
    stop that silently widens or tightens as ATR changes later is a
    different (and unannounced) risk model.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from app.execution.positions import OpenPosition
from app.strategies.base import BarContext, Condition, Signal, Strategy

RULE_TEXT_ENTRY = (
    "Enter long when close is more than {k_std} standard deviations below "
    "session VWAP and close is above the 200-period EMA (trend filter)"
)
RULE_ID_ENTRY = "vwap_reversion.oversold_long"
RULE_ID_EXIT_VWAP_TOUCH = "vwap_reversion.vwap_touch"
RULE_ID_EXIT_STOP = "vwap_reversion.stop_hit"


class VwapReversionParams(BaseModel):
    k_std: float = Field(default=2.0, gt=0)
    atr_stop_mult: float = Field(default=1.5, gt=0)


def _session_std(vwap: float | None, vwap_lower_1: float | None) -> float | None:
    """Session VWAP std, derived from the ±1 band — there's no directly
    exposed 'vwap std' field on IndicatorSnapshot (see indicators.py)."""
    if vwap is None or vwap_lower_1 is None:
        return None
    return vwap - vwap_lower_1


class VwapReversionStrategy(Strategy):
    slug = "vwap_reversion"
    timeframe = "1Min"
    default_params: dict = {"k_std": 2.0, "atr_stop_mult": 1.5}
    param_schema: type[BaseModel] = VwapReversionParams

    def _entry_conditions(self, ctx: BarContext) -> list[Condition] | None:
        """The full entry condition list, or None if indicators aren't
        warmed up enough to evaluate at all (early session / insufficient
        history) — that is a warm-up state, not a failed check, so there is
        nothing meaningful to report yet."""
        ind = ctx.indicators
        daily_ema_200 = ctx.daily_indicators.ema_200 if ctx.daily_indicators else None
        std = _session_std(ind.vwap, ind.vwap_lower_1)
        if ind.vwap is None or std is None or daily_ema_200 is None:
            return None

        close = float(ctx.bar.close)
        k_std = float(self.params["k_std"])
        vwap_threshold_price = ind.vwap - k_std * std

        oversold = Condition(
            name="vwap_oversold",
            description=(
                f"Close is more than {k_std} standard deviations below session VWAP"
            ),
            operator="<",
            threshold=vwap_threshold_price,
            actual=close,
            passed=close < vwap_threshold_price,
        )
        trend_up = Condition(
            name="trend_filter_up",
            description="Close is above the daily 200-period EMA (trend filter)",
            operator=">",
            threshold=daily_ema_200,
            actual=close,
            passed=close > daily_ema_200,
        )
        return [oversold, trend_up]

    def evaluate(self, ctx: BarContext) -> Signal | None:
        conditions = self._entry_conditions(ctx)
        if conditions is None:
            return None
        if not all(c.passed for c in conditions):
            return None

        atr = ctx.indicators.atr_14
        if atr is None:
            # Can't derive a stop distance -> can't emit a valid entry
            # signal. CLAUDE.md rule 6: every entry defines its stop.
            return None

        k_std = float(self.params["k_std"])
        atr_stop_mult = self.params["atr_stop_mult"]
        close = ctx.bar.close
        stop_price = close - Decimal(str(atr_stop_mult)) * Decimal(str(atr))
        target_price = Decimal(str(ctx.indicators.vwap))

        features = {
            "close": float(close),
            "vwap": ctx.indicators.vwap,
            "vwap_lower_1": ctx.indicators.vwap_lower_1,
            "vwap_lower_2": ctx.indicators.vwap_lower_2,
            "session_vwap_std": _session_std(
                ctx.indicators.vwap, ctx.indicators.vwap_lower_1
            ),
            "daily_ema_200": ctx.daily_indicators.ema_200 if ctx.daily_indicators else None,
            "atr_14": atr,
            "k_std": k_std,
            "atr_stop_mult": float(atr_stop_mult),
            "minutes_since_open": ctx.indicators.minutes_since_open,
            "regime": ctx.indicators.regime,
        }

        return Signal(
            side="buy",
            intent="entry",
            symbol=ctx.symbol,
            rule_id=RULE_ID_ENTRY,
            rule_text=RULE_TEXT_ENTRY.format(k_std=k_std),
            features=features,
            conditions=conditions,
            stop_price=stop_price,
            target_price=target_price,
            confidence=0.5,
        )

    def _exit_conditions(
        self, ctx: BarContext, position: OpenPosition
    ) -> tuple[Condition | None, Condition | None]:
        """Both exit checks, computed independently — neither short-circuits
        the other, so a `manage()` call that finds no exit still reports how
        close each check came."""
        close = float(ctx.bar.close)
        vwap = ctx.indicators.vwap

        vwap_touch = None
        if vwap is not None:
            vwap_touch = Condition(
                name="vwap_touch",
                description=(
                    "Close has recovered to or above the current session VWAP"
                ),
                operator=">=",
                threshold=vwap,
                actual=close,
                passed=close >= vwap,
            )

        stop_hit = None
        if position.stop_price is not None:
            stop_price = float(position.stop_price)
            stop_hit = Condition(
                name="atr_stop_hit",
                description=(
                    "Close has dropped to or below the ATR-based stop set at entry"
                ),
                operator="<=",
                threshold=stop_price,
                actual=close,
                passed=close <= stop_price,
            )

        return vwap_touch, stop_hit

    def manage(self, ctx: BarContext, position: OpenPosition) -> Signal | None:
        if position.side != "buy":
            # This strategy only ever opens longs; a non-long position
            # couldn't have come from it.
            return None

        vwap_touch, stop_hit = self._exit_conditions(ctx, position)
        conditions = [c for c in (vwap_touch, stop_hit) if c is not None]
        if not conditions:
            return None

        # Stop takes priority: it is the risk control the position was
        # sized against, so a bar that satisfies both gets reported as a
        # stop-out rather than a target-style exit.
        if stop_hit is not None and stop_hit.passed:
            rule_id = RULE_ID_EXIT_STOP
            rule_text = "Exit: close dropped to the ATR-based stop set at entry"
        elif vwap_touch is not None and vwap_touch.passed:
            rule_id = RULE_ID_EXIT_VWAP_TOUCH
            rule_text = "Exit: close recovered to the current session VWAP"
        else:
            return None

        features = {
            "close": float(ctx.bar.close),
            "vwap": ctx.indicators.vwap,
            "entry_stop_price": (
                float(position.stop_price) if position.stop_price is not None else None
            ),
            "avg_entry_price": float(position.avg_entry_price),
        }

        return Signal(
            side="sell",
            intent="exit",
            symbol=ctx.symbol,
            rule_id=rule_id,
            rule_text=rule_text,
            features=features,
            conditions=conditions,
            stop_price=None,
            target_price=None,
            confidence=1.0,
        )
