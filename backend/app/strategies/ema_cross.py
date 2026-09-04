"""EMA Crossover with regime filter strategy. BUILD_SPEC §8.3 #3.

Long-only (this codebase never shorts — BUILD_SPEC §0.3, no margin).

Entry: fast EMA(9) crosses above slow EMA(21) on the **5-minute** chart —
i.e. EMA9 was <= EMA21 on the previous finalized 5-minute bar, and is >
EMA21 on the current one. A crossover is a two-point comparison, not a
snapshot, so this needs the *previous* bar's EMA values too. `ctx.indicators`
only carries the current bar's snapshot, so we recompute the previous one
ourselves via `compute_indicators(ctx.history[:-1])` — `ctx.history` already
ends with the current bar (see `BarContext`'s docstring), so dropping the
last element gives "as of the previous bar". If `ctx.history` has fewer than
two bars there is no previous bar to compare against, so the cross can't be
detected yet; that is treated as a failed condition, not an error.

Regime filter (mandatory, not optional — BUILD_SPEC §8.3): only take longs
when the **daily** close is above the 200-day daily SMA. Needs both
`ctx.daily_indicators.sma_200` and `ctx.daily_history[-1].close`; if daily
bars haven't been fed to the engine yet (`ctx.daily_indicators is None`) or
there isn't 200 days of daily history yet (`sma_200 is None`), the regime
condition simply fails — `evaluate()` returns `None`, never raises.

Stop (BUILD_SPEC §8.3 says "recent swing low or 2 x ATR" — genuinely
ambiguous about which one wins, so here is the documented, deliberate
choice this module makes): compute the lowest low over the last
`swing_lookback` bars (default 10, including the entry bar itself) as the
swing-low candidate, and `entry_price - stop_atr_mult * ATR(14)` (default
multiplier 2.0) as the ATR candidate. Use whichever is **lower** — i.e.
further from entry, the wider stop — so this strategy never ends up with an
unrealistically tight stop on a quiet swing low right under the entry.
CLAUDE.md rule 6: every entry Signal from this strategy has a `stop_price`;
there is always at least one candidate (the swing low, over however many
bars are actually available), so this never returns `None`.

Trail (BUILD_SPEC §8.3 "trail at 1.5 x ATR after 1R is reached" — the exact
rule this module implements, stated precisely): once any bar since the
position opened has traded (by its `high`) at or above `entry + 1R` (1R =
`entry - initial_stop`), the effective stop becomes the running maximum,
over every bar from the first such 1R-touch onward, of
`bar.close - trail_atr_mult * ATR(14)` (default multiplier 1.5) — floored at
the initial stop so it never trails down. `manage()` is stateless (matches
`orb.py`'s pattern): it recomputes this from `position` + `ctx.history` on
every call rather than keeping mutable strategy state. One simplification,
documented here because it deviates from a literal per-bar ATR: the trail
candidates all use the **current** bar's ATR(14) (`ctx.indicators.atr_14`)
rather than each historical bar's own ATR at the time — recomputing ATR at
every historical bar would require re-running the indicator pipeline once
per bar in the window, which is unnecessary weight for what is, per
BUILD_SPEC, deliberately a strategy we expect to underperform in chop.

BUILD_SPEC explicitly wants this strategy left "as-is" so it can be watched
whipsawing in choppy markets — no extra chop-avoidance filter is added
beyond the crossover + daily SMA200 regime filter actually specified.
`ctx.indicators.regime` is surfaced in `features` as informational context
only, never as a gating condition.
"""

from __future__ import annotations

from decimal import Decimal

from app.execution.positions import OpenPosition
from app.strategies.base import BarContext, Condition, Signal, Strategy
from app.strategies.indicators import compute_indicators


def _dec(value: float) -> Decimal:
    """float indicator value -> Decimal, via str to avoid binary-float noise."""
    return Decimal(str(value))


class EmaCrossoverRegimeFilter(Strategy):
    slug = "ema_cross"
    # BUILD_SPEC §8.3: the crossover itself runs on the 5-minute chart. The
    # regime filter is daily, via ctx.daily_history/daily_indicators.
    timeframe = "5Min"
    default_params: dict = {
        "swing_lookback": 10,
        "stop_atr_mult": 2.0,
        "trail_atr_mult": 1.5,
    }

    # -- conditions, computed once, independent of the final decision -----

    def _crossover_conditions(self, ctx: BarContext) -> tuple[Condition, Condition] | None:
        """The two atomic checks that together make a "fresh cross":

        1. EMA9 was NOT already above EMA21 on the previous bar (otherwise
           this is a trend continuing, not a fresh cross).
        2. EMA9 IS above EMA21 on the current bar.

        Both are reported, even if the strategy ultimately doesn't trade,
        per BUILD_SPEC §8.2's "full condition list, including failures" —
        but only once there's enough history to form real values. Returns
        `None` (mirroring the other three strategies' `_conditions` helpers)
        when fewer than 2 bars are available or the EMAs haven't warmed up
        yet: that's a warm-up state with nothing yet to report, not a failed
        check, and it keeps placeholder/sentinel values out of anything that
        might eventually be persisted to `signals.conditions` (JSONB has no
        native NaN).
        """
        ind = ctx.indicators
        ema9_now = ind.ema_9
        ema21_now = ind.ema_21
        if ema9_now is None or ema21_now is None or len(ctx.history) < 2:
            return None

        prior_snapshot = compute_indicators(ctx.history[:-1])
        prior_ema9 = prior_snapshot.ema_9
        prior_ema21 = prior_snapshot.ema_21
        if prior_ema9 is None or prior_ema21 is None:
            return None

        not_crossed_prior = Condition(
            name="ema9_not_above_ema21_prior_bar",
            description=(
                "Fast EMA(9) was NOT already above slow EMA(21) on the previous "
                "finalized bar -- required so this is a fresh cross, not an "
                "already-crossed trend continuing"
            ),
            operator="<=",
            threshold=prior_ema21,
            actual=prior_ema9,
            passed=prior_ema9 <= prior_ema21,
        )
        above_now = Condition(
            name="ema9_above_ema21_now",
            description="Fast EMA(9) is above slow EMA(21) on the current bar",
            operator=">",
            threshold=ema21_now,
            actual=ema9_now,
            passed=ema9_now > ema21_now,
        )
        return not_crossed_prior, above_now

    def _regime_condition(self, ctx: BarContext) -> Condition | None:
        """Mandatory daily regime filter (BUILD_SPEC §8.3): daily close > SMA(200).

        Returns `None` (never raises) when daily bars haven't been fed to
        the engine yet (`ctx.daily_indicators is None`) or there isn't 200
        days of history yet (`sma_200 is None`) — same warm-up convention as
        `_crossover_conditions` above.
        """
        daily_ind = ctx.daily_indicators
        daily_hist = ctx.daily_history
        sma_200 = daily_ind.sma_200 if daily_ind is not None else None
        if sma_200 is None or not daily_hist:
            return None
        daily_close = float(daily_hist[-1].close)

        return Condition(
            name="daily_close_above_sma_200",
            description=(
                "Daily close is above the 200-day daily SMA -- the mandatory "
                "long-only regime filter"
            ),
            operator=">",
            threshold=sma_200,
            actual=daily_close,
            passed=daily_close > sma_200,
        )

    def _stop_price(self, ctx: BarContext, entry_price: Decimal) -> Decimal:
        lookback = self.params["swing_lookback"]
        window = ctx.history[-lookback:] if len(ctx.history) >= lookback else ctx.history
        swing_low = min(b.low for b in window)

        candidates = [swing_low]
        atr = ctx.indicators.atr_14
        if atr is not None:
            candidates.append(entry_price - _dec(self.params["stop_atr_mult"] * atr))

        # Whichever is LOWER is further from entry -> the wider, safer stop.
        return min(candidates)

    def evaluate(self, ctx: BarContext) -> Signal | None:
        crossover = self._crossover_conditions(ctx)
        if crossover is None:
            return None
        regime_ok = self._regime_condition(ctx)
        if regime_ok is None:
            return None

        not_crossed_prior, above_now = crossover
        conditions = [not_crossed_prior, above_now, regime_ok]
        if not all(c.passed for c in conditions):
            return None

        ind = ctx.indicators
        entry_price = ctx.bar.close
        stop_price = self._stop_price(ctx, entry_price)  # CLAUDE.md rule 6: always set

        return Signal(
            side="buy",
            intent="entry",
            symbol=ctx.symbol,
            rule_id="ema_cross.golden_cross_long",
            rule_text=(
                "Fast EMA(9) crossed above slow EMA(21) on the 5-minute chart "
                "(was at or below it the previous bar), and the daily close is "
                "above the 200-day SMA."
            ),
            features={
                "close": float(entry_price),
                "ema_9": ind.ema_9,
                "ema_21": ind.ema_21,
                "atr_14": ind.atr_14,
                "daily_sma_200": ctx.daily_indicators.sma_200 if ctx.daily_indicators else None,
                "daily_close": float(ctx.daily_history[-1].close) if ctx.daily_history else None,
                "regime": ind.regime,  # informational only -- not a gating condition
            },
            conditions=conditions,
            stop_price=stop_price,
        )

    def manage(self, ctx: BarContext, position: OpenPosition) -> Signal | None:
        """Exit logic for an open ema_cross long: stop, or ATR trail after 1R.

        See the module docstring for the exact, documented trail rule. No
        fixed profit target is defined for this strategy in BUILD_SPEC §8.3
        (unlike ORB/VWAP reversion) -- the only planned exit is the stop,
        which widens (trails) once 1R is reached.
        """
        entry = position.avg_entry_price
        initial_stop = position.stop_price
        if initial_stop is None:
            # Shouldn't happen for a position this strategy opened (rule 6
            # guarantees stop_price on entry) -- nothing to manage on.
            return None

        risk = entry - initial_stop
        one_r_price = entry + risk

        bars_since_open = [b for b in ctx.history if b.ts >= position.opened_at]
        reached_idx = next(
            (i for i, b in enumerate(bars_since_open) if b.high >= one_r_price), None
        )
        reached_1r = reached_idx is not None

        effective_stop = initial_stop
        atr = ctx.indicators.atr_14
        if reached_1r and atr is not None:
            trail_amount = _dec(self.params["trail_atr_mult"] * atr)
            trail_candidates = [b.close - trail_amount for b in bars_since_open[reached_idx:]]
            effective_stop = max(initial_stop, max(trail_candidates))

        if ctx.bar.low > effective_stop:
            return None

        rule_id = "ema_cross.trail_stop" if reached_1r else "ema_cross.stop_hit"
        rule_text = (
            "Price pulled back to the ATR-based trailing stop after reaching 1R."
            if reached_1r
            else "Price hit the initial stop (swing low or 2x ATR, whichever was wider)."
        )
        return Signal(
            side="sell",
            intent="exit",
            symbol=ctx.symbol,
            rule_id=rule_id,
            rule_text=rule_text,
            features={
                "close": float(ctx.bar.close),
                "bar_low": float(ctx.bar.low),
                "avg_entry_price": float(entry),
                "initial_stop": float(initial_stop),
                "effective_stop": float(effective_stop),
                "reached_1r": reached_1r,
                "atr_14": atr,
            },
            conditions=[
                Condition(
                    name="bar_low_at_or_below_effective_stop",
                    description="Bar low touched the (possibly trailed) stop",
                    operator="<=",
                    threshold=float(effective_stop),
                    actual=float(ctx.bar.low),
                    passed=True,
                )
            ],
            stop_price=None,
        )
