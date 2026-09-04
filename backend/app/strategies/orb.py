"""Opening Range Breakout strategy. BUILD_SPEC §8.3 #1.

Long-only (this codebase never shorts — BUILD_SPEC §0.3, no margin). BUILD_SPEC's
"short-equivalent (exit/avoid) below range low" is read as a reason to *avoid
entering*, not a short position: a close below the opening range low simply
fails the breakout condition below, so no entry fires.

Entry: close above the opening range high, filtered by (a) a minimum range
width (as a multiple of ATR(14)) and (b) elevated volume. BUILD_SPEC §8.3
literally asks for "volume above the 20-day average for that time of day" — a
true 20-*trading-day*, same-time-of-day average needs daily-bar history this
strategy doesn't have on its own 1Min timeframe (`ctx.daily_history` exists,
but it's daily *bars*, not a per-minute-of-day volume profile). We use
`ctx.indicators.relative_volume_20` — current bar volume vs. its own rolling
20-*bar* average — as a documented, deliberate simplification of that filter.
This is a conscious scope decision, not a misreading of §8.3.

Every entry defines its stop (CLAUDE.md rule 6): stop is the opposite side of
the opening range (`opening_range_low`), so a strategy that can't compute a
stop (no opening range yet) simply cannot enter — see `_conditions` below.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from app.execution.positions import OpenPosition
from app.market_calendar import NY
from app.strategies.base import BarContext, Condition, Signal, Strategy

# BUILD_SPEC §8.3: "Time exit: flat by 15:55 ET."
_TIME_EXIT = time(15, 55)


def _dec(value: float) -> Decimal:
    """float indicator value -> Decimal, via str to avoid binary-float noise."""
    return Decimal(str(value))


class OpeningRangeBreakout(Strategy):
    slug = "orb"
    # BUILD_SPEC §8.3: ORB trades the 1-minute chart.
    timeframe = "1Min"
    default_params: dict = {
        # Informational only: the opening-range *window* itself (first N
        # minutes of the session) is computed by app.strategies.indicators
        # (`_OPENING_RANGE_MINUTES`), which this strategy consumes via
        # `ctx.indicators.opening_range_high/low` rather than recomputing.
        "opening_range_minutes": 15,
        "min_range_atr_mult": 0.5,
        "min_relative_volume": 1.2,
    }

    def _conditions(self, ctx: BarContext) -> list[Condition] | None:
        """The full condition list for a potential long entry on this bar.

        Computed once, independent of whether the final decision ends up
        being "signal" or "no signal" — BUILD_SPEC §8.2. Returns None only
        when the inputs needed to even form the conditions aren't available
        yet (opening range/ATR/relative-volume still warming up); in that
        case no entry is possible either way, so there is nothing to report.
        """
        ind = ctx.indicators
        or_high = ind.opening_range_high
        or_low = ind.opening_range_low
        atr = ind.atr_14
        rel_vol = ind.relative_volume_20
        if or_high is None or or_low is None or atr is None or rel_vol is None:
            return None

        close = float(ctx.bar.close)
        range_width = or_high - or_low
        min_mult = self.params["min_range_atr_mult"]
        if atr > 0:
            range_atr_mult = range_width / atr
        else:
            # Degenerate (zero-ATR) case: any positive width trivially clears
            # a zero floor; a zero width does not.
            range_atr_mult = float("inf") if range_width > 0 else 0.0

        min_rel_vol = self.params["min_relative_volume"]

        breakout = Condition(
            name="close_above_range_high",
            description="Bar closes above the opening range high",
            operator=">",
            threshold=or_high,
            actual=close,
            passed=close > or_high,
        )
        width_ok = Condition(
            name="range_width_min_atr_mult",
            description=(
                "Opening range width, as a multiple of ATR(14), meets the "
                "minimum (filters out days where the range is too tight to "
                "be meaningful)"
            ),
            operator=">=",
            threshold=min_mult,
            actual=range_atr_mult,
            passed=range_atr_mult >= min_mult,
        )
        volume_ok = Condition(
            name="relative_volume_min",
            description=(
                "Bar volume, relative to its own rolling 20-bar average, "
                "meets the minimum (deliberate simplification of BUILD_SPEC "
                "§8.3's '20-day average for that time of day' — see module "
                "docstring)"
            ),
            operator=">=",
            threshold=min_rel_vol,
            actual=rel_vol,
            passed=rel_vol >= min_rel_vol,
        )
        return [breakout, width_ok, volume_ok]

    def evaluate(self, ctx: BarContext) -> Signal | None:
        conditions = self._conditions(ctx)
        if conditions is None or not all(c.passed for c in conditions):
            return None

        ind = ctx.indicators
        entry_price = ctx.bar.close
        stop_price = _dec(ind.opening_range_low)  # CLAUDE.md rule 6: always set
        risk = entry_price - stop_price
        target_price = entry_price + 2 * risk  # BUILD_SPEC §8.3: target 2R

        return Signal(
            side="buy",
            intent="entry",
            symbol=ctx.symbol,
            rule_id="orb.breakout_long",
            rule_text=(
                "Close broke above the opening range high, on a range wide "
                "enough (vs. ATR) to be meaningful and on elevated volume."
            ),
            features={
                "close": float(entry_price),
                "opening_range_high": ind.opening_range_high,
                "opening_range_low": ind.opening_range_low,
                "atr_14": ind.atr_14,
                "relative_volume_20": ind.relative_volume_20,
            },
            conditions=conditions,
            stop_price=stop_price,
            target_price=target_price,
        )

    def manage(self, ctx: BarContext, position: OpenPosition) -> Signal | None:
        """Exit logic for an open ORB long.

        Priority each bar (documented, deliberate order — see comments):
        1. Time exit: flat by 15:55 ET, regardless of P&L.
        2. Stop / trailed stop.
        3. Target (2R).

        Trailing rule (BUILD_SPEC §8.3 "trail after 1R"): once *any* bar
        since the position opened has traded at or above entry + 1R, the
        effective stop becomes max(original_stop, entry_price) — i.e. it
        trails to breakeven and never gives that back. This is recomputed
        from `position` + `ctx.history` on every call rather than kept as
        mutable strategy state, matching the stateless style of `evaluate`.
        """
        local_time = ctx.bar.ts.astimezone(NY).time()
        if local_time >= _TIME_EXIT:
            return self._exit(
                ctx, position,
                rule_id="orb.time_exit",
                rule_text="Flat by 15:55 ET, regardless of P&L.",
                trigger_name="time_at_or_past_1555_et",
                trigger_description=(
                    "Bar time (America/New_York) is at or past the 15:55 flat-by time"
                ),
                threshold=_TIME_EXIT.hour * 60 + _TIME_EXIT.minute,
                actual=local_time.hour * 60 + local_time.minute,
            )

        entry = position.avg_entry_price
        stop = position.stop_price
        if stop is None:
            # Shouldn't happen for a position this strategy opened (rule 6
            # guarantees stop_price on entry) — nothing more to manage on.
            return None

        risk = entry - stop
        target = entry + 2 * risk
        one_r_price = entry + risk

        bars_since_open = [b for b in ctx.history if b.ts >= position.opened_at]
        reached_1r = any(b.high >= one_r_price for b in bars_since_open)
        effective_stop = max(stop, entry) if reached_1r else stop
        stop_rule_id = "orb.trail_stop" if reached_1r else "orb.stop_hit"
        stop_rule_text = (
            "Price fell back to the breakeven-or-better stop after reaching 1R."
            if reached_1r
            else "Price hit the original opening-range stop."
        )

        # Stop before target: a bar wide enough to touch both in one print
        # is treated conservatively, protecting capital first.
        if ctx.bar.low <= effective_stop:
            return self._exit(
                ctx, position,
                rule_id=stop_rule_id,
                rule_text=stop_rule_text,
                trigger_name="low_at_or_below_stop",
                trigger_description="Bar low touched the (possibly trailed) stop",
                threshold=float(effective_stop),
                actual=float(ctx.bar.low),
            )

        if ctx.bar.high >= target:
            return self._exit(
                ctx, position,
                rule_id="orb.target_2r",
                rule_text="Price reached the 2R target.",
                trigger_name="high_at_or_above_target",
                trigger_description="Bar high touched the 2R target",
                threshold=float(target),
                actual=float(ctx.bar.high),
            )

        return None

    @staticmethod
    def _exit(
        ctx: BarContext,
        position: OpenPosition,
        *,
        rule_id: str,
        rule_text: str,
        trigger_name: str,
        trigger_description: str,
        threshold: float,
        actual: float,
    ) -> Signal:
        condition = Condition(
            name=trigger_name,
            description=trigger_description,
            operator=">=",
            threshold=threshold,
            actual=actual,
            passed=True,
        )
        return Signal(
            side="sell",
            intent="exit",
            symbol=ctx.symbol,
            rule_id=rule_id,
            rule_text=rule_text,
            features={
                "close": float(ctx.bar.close),
                "bar_high": float(ctx.bar.high),
                "bar_low": float(ctx.bar.low),
                "avg_entry_price": float(position.avg_entry_price),
                "stop_price": (
                    float(position.stop_price) if position.stop_price is not None else None
                ),
            },
            conditions=[condition],
            stop_price=None,
        )
