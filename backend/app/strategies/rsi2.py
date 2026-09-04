"""RSI(2) Mean Reversion strategy. BUILD_SPEC §8.3 #4.

Larry Connors' classic mean-reversion system. Unlike the other three
starter strategies, this one is fundamentally a **daily-bar** strategy —
BUILD_SPEC's "200-day SMA", "5-day SMA", and "5 days" hard time stop only
make sense measured in daily bars. `Strategy.timeframe` here is `"1Day"`,
so `ctx.indicators` (computed by `app.strategies.engine.SymbolEngine` over
this strategy's own 1Day history) already gives us `rsi_2`, `sma_200`,
`sma_5`, and `atr_14` directly. `ctx.daily_history`/`ctx.daily_indicators`
exist on `BarContext` only for strategies whose *primary* timeframe isn't
daily (EMA crossover, VWAP reversion) — since `bar.timeframe == "1Day"`
here, those fields are always `None` and this module never touches them.

Entry (long only — this codebase never shorts, no margin, BUILD_SPEC §0.3):
RSI(2) < oversold threshold (default 10) AND close > SMA(200) (trend filter).

Exit: RSI(2) > overbought threshold (default 70) OR close above SMA(5).
Either condition alone is sufficient — `manage()` checks both independently.

Hard time stop: 5 *trading* days. BUILD_SPEC §8.3 just says "5 days" without
disambiguating trading vs. calendar days. Since this strategy's bars ARE
daily bars, "trading days via bar count" is the more spec-faithful reading
than calendar days: `manage()` counts finalized bars in `ctx.history` with
`ts > position.opened_at` (i.e. daily bars that have closed since entry,
excluding the entry bar itself) and forces an exit once that count reaches
`time_stop_bars` (default 5), regardless of what RSI(2)/SMA(5) say. Calendar
days would silently let weekends/holidays shrink the intended holding
window, which isn't what "5 days" of a daily-bar strategy should mean.

Stop price — deviation from BUILD_SPEC §8.3, required by CLAUDE.md rule 6:
the spec's RSI(2) paragraph defines no price-based stop at all (only the
RSI>70 / above-SMA5 exit and the 5-day time stop). CLAUDE.md rule 6 is a
blanket, non-negotiable constraint: every entry Signal MUST set
`stop_price`, or the risk engine rejects it outright with no exceptions. To
satisfy that rule while staying consistent with the other three strategies
in this codebase (orb, vwap_reversion, ema_cross — all ATR-based), entries
here set `stop_price = entry_price - atr_stop_mult * ATR(14)` (default
multiplier 1.5). This is an addition on top of BUILD_SPEC §8.3's own
description of RSI(2), not part of it.
"""

from __future__ import annotations

from decimal import Decimal

from app.execution.positions import OpenPosition
from app.strategies.base import BarContext, Condition, Signal, Strategy


def _dec(value: float) -> Decimal:
    """float indicator value -> Decimal, via str to avoid binary-float noise."""
    return Decimal(str(value))


class Rsi2MeanReversion(Strategy):
    slug = "rsi2"
    # BUILD_SPEC §8.3: RSI(2) is a daily-bar strategy (200-day SMA, 5-day
    # SMA, 5-day time stop all presuppose daily bars).
    timeframe = "1Day"
    default_params: dict = {
        "rsi_oversold_threshold": 10.0,
        "rsi_overbought_threshold": 70.0,
        # Not part of BUILD_SPEC §8.3's RSI(2) description — see module
        # docstring. Added solely to satisfy CLAUDE.md rule 6.
        "atr_stop_mult": 1.5,
        # BUILD_SPEC §8.3 "hard time stop at 5 days", read here as 5
        # trading days (finalized daily bars) — see module docstring.
        "time_stop_bars": 5,
    }

    def _conditions(self, ctx: BarContext) -> list[Condition] | None:
        """The full entry condition list for this bar, computed once,
        independent of whether the final decision is "signal" or "no
        signal" — BUILD_SPEC §8.2. Returns None only when the indicators
        needed to even form the conditions aren't warmed up yet (RSI(2) or
        SMA(200) still None on insufficient history); in that case no entry
        is possible either way, so there is nothing to report.
        """
        ind = ctx.indicators
        rsi2 = ind.rsi_2
        sma200 = ind.sma_200
        if rsi2 is None or sma200 is None:
            return None

        close = float(ctx.bar.close)
        oversold_threshold = self.params["rsi_oversold_threshold"]

        rsi_condition = Condition(
            name="rsi2_below_oversold",
            description="RSI(2) is below the oversold threshold",
            operator="<",
            threshold=float(oversold_threshold),
            actual=rsi2,
            passed=rsi2 < oversold_threshold,
        )
        trend_condition = Condition(
            name="close_above_sma200",
            description="Close is above the 200-day SMA (trend filter)",
            operator=">",
            threshold=sma200,
            actual=close,
            passed=close > sma200,
        )
        return [rsi_condition, trend_condition]

    def evaluate(self, ctx: BarContext) -> Signal | None:
        conditions = self._conditions(ctx)
        if conditions is None or not all(c.passed for c in conditions):
            return None

        ind = ctx.indicators
        atr14 = ind.atr_14
        if atr14 is None:
            # Shouldn't happen once SMA(200) has warmed up — ATR(14) needs
            # far fewer bars — but CLAUDE.md rule 6 requires a stop and
            # there's nothing to compute one from yet, so no signal rather
            # than a bad one.
            return None

        entry_price = ctx.bar.close
        stop_mult = self.params["atr_stop_mult"]
        # CLAUDE.md rule 6 addition — see module docstring. Not part of
        # BUILD_SPEC §8.3's own description of this strategy.
        stop_price = entry_price - _dec(atr14 * stop_mult)

        oversold_threshold = self.params["rsi_oversold_threshold"]
        return Signal(
            side="buy",
            intent="entry",
            symbol=ctx.symbol,
            rule_id="rsi2.oversold_long",
            rule_text=(
                f"RSI(2) closed below {oversold_threshold} while price closed "
                "above the 200-day SMA (Larry Connors' RSI(2) mean reversion)."
            ),
            features={
                "close": float(entry_price),
                "rsi_2": ind.rsi_2,
                "sma_200": ind.sma_200,
                "atr_14": atr14,
            },
            conditions=conditions,
            stop_price=stop_price,
        )

    def manage(self, ctx: BarContext, position: OpenPosition) -> Signal | None:
        """Exit logic for an open RSI(2) long.

        Checked in this order (documented, deliberate):
        1. RSI(2) > overbought threshold.
        2. Close above SMA(5).
        3. Hard time stop: `time_stop_bars` trading days elapsed since entry.

        BUILD_SPEC §8.3 lists (1) and (2) as independent, either-is-enough
        exits, and (3) as a hard stop that fires "regardless" of the other
        two — so it is checked last, as the fallback that guarantees an
        exit even when neither RSI(2) nor SMA(5) ever trigger.
        """
        ind = ctx.indicators
        rsi2 = ind.rsi_2
        sma5 = ind.sma_5
        close = float(ctx.bar.close)

        overbought_threshold = self.params["rsi_overbought_threshold"]
        if rsi2 is not None and rsi2 > overbought_threshold:
            return self._exit(
                ctx,
                rule_id="rsi2.overbought_exit",
                rule_text=(
                    f"RSI(2) closed above the overbought threshold "
                    f"({overbought_threshold})."
                ),
                condition=Condition(
                    name="rsi2_above_overbought",
                    description="RSI(2) is above the overbought threshold",
                    operator=">",
                    threshold=float(overbought_threshold),
                    actual=rsi2,
                    passed=True,
                ),
            )

        if sma5 is not None and close > sma5:
            return self._exit(
                ctx,
                rule_id="rsi2.above_sma5_exit",
                rule_text="Price closed above the 5-day SMA.",
                condition=Condition(
                    name="close_above_sma5",
                    description="Close is above the 5-day SMA",
                    operator=">",
                    threshold=sma5,
                    actual=close,
                    passed=True,
                ),
            )

        # Hard time stop: trading days (finalized daily bars) elapsed since
        # entry, counted from `ctx.history` rather than kept as mutable
        # strategy state — matches the stateless style of `evaluate`. The
        # entry bar itself (ts == opened_at) is not counted.
        time_stop_bars = self.params["time_stop_bars"]
        bars_since_open = sum(1 for b in ctx.history if b.ts > position.opened_at)
        if bars_since_open >= time_stop_bars:
            return self._exit(
                ctx,
                rule_id="rsi2.time_stop",
                rule_text=(
                    f"Hard time stop: {time_stop_bars} trading days elapsed "
                    "since entry."
                ),
                condition=Condition(
                    name="bars_since_open_at_time_stop",
                    description=(
                        "Trading days (finalized daily bars) elapsed since "
                        "entry reached the hard time-stop limit"
                    ),
                    operator=">=",
                    threshold=float(time_stop_bars),
                    actual=float(bars_since_open),
                    passed=True,
                ),
            )

        return None

    @staticmethod
    def _exit(
        ctx: BarContext,
        *,
        rule_id: str,
        rule_text: str,
        condition: Condition,
    ) -> Signal:
        return Signal(
            side="sell",
            intent="exit",
            symbol=ctx.symbol,
            rule_id=rule_id,
            rule_text=rule_text,
            features={
                "close": float(ctx.bar.close),
                "rsi_2": ctx.indicators.rsi_2,
                "sma_5": ctx.indicators.sma_5,
            },
            conditions=[condition],
            stop_price=None,
        )
