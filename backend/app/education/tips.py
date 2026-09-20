"""The trading tip library and selector. BUILD_SPEC §11.3.

Tips are matched against a **trade context** dict — plain scalars derived
from a closed `Trade` row (and its linked `SignalRecord`s, when they exist)
by `app/education/explainer.py`. This module never touches the database and
never touches price history; it only evaluates the rules below against
whatever context dict it's handed, which keeps it trivially unit-testable
and keeps the "never fabricate, never predict" boundary in one place
(the explainer, not here).

Tonight's real-world honesty note: every trade placed through this build
goes through `POST /orders` manually (see `app/execution/order_service.py`
and `app/models/positions.py`'s own docstring — nothing auto-executes a
strategy signal into a real position yet). That means metrics that would
come from a linked `SignalRecord`'s feature snapshot (`mfe_r`,
`stop_distance_atr`, `followed_rules`) are `None` for every trade tonight,
and any trigger rule that reads them simply won't match (`_matches` treats a
missing/`None` metric as "condition not satisfied", never as a wildcard).
Those tips are still shipped, correctly implemented, and will start
triggering the moment a future phase links signals to trades — they are not
dead code, they're forward-compatible.

### Deviation, documented per CLAUDE.md's convention

BUILD_SPEC §11.3's suppression rule is: "suppress any tip shown in the last
10 trades unless it triggers three times in a row." Read literally, "triggers"
is independent of "shown" — a tip can trigger while suppressed. Reproducing
that exactly requires tracking every tip's trigger-match history regardless
of what was actually displayed, which `app/education/explainer.py` does by
reconstructing the trade context for the small, fixed window of prior trades
it needs (see that module's `_consecutive_trigger_counts`) — not by adding a
new "all trigger matches" column, which would be schema growth this phase
doesn't otherwise need. That reconstruction is exact for metrics derivable
from `trades`/`signals` rows (all of them, tonight); it is documented here
because it is the one part of §11.3 that isn't a pure, context-in/tip-out
function like the rest of this module.
"""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}

# BUILD_SPEC §11.3: "rank matches by category priority (risk > psychology >
# costs > execution > strategy)". Lower number = higher priority.
CATEGORY_PRIORITY: dict[str, int] = {
    "risk": 0,
    "psychology": 1,
    "costs": 2,
    "execution": 3,
    "strategy": 4,
}

# How many of the most recent trades count as "shown in the last 10 trades".
SUPPRESSION_WINDOW = 10
# "unless it triggers three times in a row".
SUPPRESSION_OVERRIDE_STREAK = 3


@dataclass(frozen=True)
class Condition:
    metric: str
    op: str  # one of _OPS
    value: Any

    def evaluate(self, context: Mapping[str, Any]) -> bool:
        if self.metric not in context:
            return False
        actual = context[self.metric]
        if actual is None:
            return False
        try:
            return bool(_OPS[self.op](actual, self.value))
        except TypeError:
            # Comparing incompatible types (e.g. a string metric against a
            # numeric threshold) is a data-shape bug, not a match -- fail
            # closed rather than raise out of a selection call.
            return False


@dataclass(frozen=True)
class Tip:
    id: str
    category: str  # 'risk' | 'psychology' | 'costs' | 'execution' | 'strategy' | 'fundamentals'
    title: str
    body: str
    # `all()` semantics over these conditions. Empty = never trigger-matched;
    # used only for the `fundamentals` rotation pool (see `select_tip`).
    conditions: tuple[Condition, ...] = field(default_factory=tuple)

    def matches(self, context: Mapping[str, Any]) -> bool:
        if not self.conditions:
            return False
        return all(c.evaluate(context) for c in self.conditions)

    def render(self, context: Mapping[str, Any]) -> str:
        """Fill `{metric}` placeholders in `body` from the context, per the
        BUILD_SPEC §11.3 `costs.frequency_drag` example ("across {trades_today}
        trades"). Falls back to the literal template on a missing key rather
        than raising -- a tip must never crash explanation generation."""
        try:
            return self.body.format(**context)
        except (KeyError, IndexError):
            return self.body


def _c(metric: str, op: str, value: Any) -> Condition:
    return Condition(metric=metric, op=op, value=value)


# ---------------------------------------------------------------------------
# The tip library.
#
# The four tips marked VERBATIM are copied exactly from BUILD_SPEC §11.3 --
# id, category, trigger_rule, and body -- as instructed. Everything else is
# new, written for this phase, honestly counted (see the Phase 5 summary):
# 16 more trigger-based tips across the same five categories, plus a
# separate rotating "fundamentals" pool for when nothing matches.
# ---------------------------------------------------------------------------

TIPS: tuple[Tip, ...] = (
    # ---- risk ----------------------------------------------------------
    Tip(  # VERBATIM — BUILD_SPEC §11.3
        id="risk.stop_too_tight",
        category="risk",
        title="Your stop may be inside the noise",
        conditions=(
            _c("exit_reason", "eq", "stop"),
            _c("mfe_r", "gt", 0.8),
            _c("stop_distance_atr", "lt", 1.0),
        ),
        body=(
            "This trade moved 0.8R in your favour before stopping out, and your stop sat "
            "less than one ATR away. A stop inside the average bar range gets hit by "
            "ordinary noise rather than by your thesis being wrong. Size smaller and stop "
            "wider — the risk in dollars stays identical."
        ),
    ),
    Tip(
        id="risk.stop_hit_with_slippage",
        category="risk",
        title="This loss ran past your planned risk",
        conditions=(_c("exit_reason", "eq", "stop"), _c("r_multiple", "lt", -1.1)),
        body=(
            "Your stop fired, but the trade closed at {r_multiple:.2f}R — worse than the "
            "-1.0R a clean stop should produce. The gap between plan and outcome usually "
            "comes from a gap-through or a market order filling past the trigger price, "
            "not from the stop level being wrong."
        ),
    ),
    Tip(
        id="risk.compounding_losses",
        category="risk",
        title="Three losses in a row, same process",
        conditions=(_c("consecutive_losses", "gte", 3), _c("source", "eq", "manual")),
        body=(
            "This is your third consecutive loss with nothing recorded that changed "
            "between them — same manual entry process each time. Streaks like this are "
            "exactly what a cooldown or a size step-down exists for; the goal isn't to "
            "predict the next trade, it's to stop compounding the same exposure."
        ),
    ),
    Tip(
        id="risk.oversized_loss",
        category="risk",
        title="This loss exceeded one unit of planned risk",
        conditions=(_c("r_multiple", "lte", -1.3),),
        body=(
            "This trade lost {r_multiple:.2f}R. A trade that respects its own stop caps "
            "the loss at -1.0R; anything materially past that means the exit executed "
            "later, or at a worse price, than the plan called for — worth checking "
            "whether the stop order type is doing what you expect."
        ),
    ),
    Tip(
        id="risk.win_broke_the_rule",
        category="risk",
        title="This win did not follow the recorded rule",
        conditions=(_c("net_pnl", "gt", 0), _c("followed_rules", "eq", False)),
        body=(
            "This trade made {net_pnl:.2f}, but the recorded entry conditions were not "
            "satisfied when it was taken. A win that broke the process is not evidence "
            "the process works — track it as a broken-rule trade, not a good one, or the "
            "sample you're learning from is contaminated."
        ),
    ),
    # ---- psychology ------------------------------------------------------
    Tip(  # VERBATIM — BUILD_SPEC §11.3
        id="psychology.revenge_trade",
        category="psychology",
        title="This looks like a revenge trade",
        conditions=(
            _c("consecutive_losses", "gte", 2),
            _c("seconds_since_last_exit", "lt", 120),
            _c("source", "eq", "manual"),
        ),
        body=(
            "You opened this manually within two minutes of a second consecutive loss. "
            "That timing is the classic signature of a revenge trade. The cooldown timer "
            "exists for this exact moment — let it run."
        ),
    ),
    Tip(  # VERBATIM — BUILD_SPEC §11.3 (id kept as "process." per the spec's own YAML)
        id="process.good_loss",
        category="psychology",
        title="This was a good trade that lost money",
        conditions=(
            _c("net_pnl", "lt", 0),
            _c("followed_rules", "eq", True),
            _c("r_multiple", "gte", -1.05),
        ),
        body=(
            "This was a good trade. It lost money, and it lost exactly the amount you "
            "decided to risk, for the reason you planned for. Separating decision quality "
            "from outcome is the skill that takes longest to build and matters most."
        ),
    ),
    Tip(
        id="psychology.cut_winner_short",
        category="psychology",
        title="This winner was closed almost immediately",
        conditions=(
            _c("exit_reason", "eq", "manual"),
            _c("net_pnl", "gt", 0),
            _c("hold_minutes", "lt", 2),
        ),
        body=(
            "This position was manually closed for a gain within {hold_minutes:.1f} "
            "minutes of being opened. An exit that fast is rarely driven by a plan — it's "
            "usually the discomfort of an open position, taken out the moment it turns "
            "green. Worth asking whether the exit matched a target, or just a feeling."
        ),
    ),
    Tip(
        id="psychology.fast_reentry",
        category="psychology",
        title="You re-entered within a minute of your last exit",
        conditions=(_c("source", "eq", "manual"), _c("seconds_since_last_exit", "lt", 60)),
        body=(
            "Less than a minute passed between your last exit and this entry. That's not "
            "enough time to have reviewed what the last trade actually did — it's the "
            "pace of reacting, not deciding. Even a losing trade deserves a beat before "
            "the next one."
        ),
    ),
    Tip(
        id="psychology.busy_session",
        category="psychology",
        title="This was trade number {trades_today} today",
        conditions=(_c("trades_today", "gte", 8),),
        body=(
            "This is at least your {trades_today}th trade today. Decision quality tends "
            "to degrade with volume of decisions, independent of skill — fatigue looks "
            "like impatience from the inside. Worth noticing on a day like this one."
        ),
    ),
    # ---- costs -------------------------------------------------------
    Tip(  # VERBATIM — BUILD_SPEC §11.3
        id="costs.frequency_drag",
        category="costs",
        title="Friction is eating today's gross P&L",
        conditions=(
            _c("trades_today", "gte", 8),
            _c("friction_pct_of_gross_today", "gt", 0.30),
        ),
        body=(
            "Friction ate more than 30% of your gross P&L today across {trades_today} "
            "trades. At this rate your strategy has to be right substantially more often "
            "just to break even. Fewer, higher-conviction trades beat more trades at "
            "almost every skill level."
        ),
    ),
    Tip(
        id="costs.friction_ate_the_edge",
        category="costs",
        title="Friction turned a winning trade into a loss",
        conditions=(_c("net_pnl", "lt", 0), _c("gross_pnl", "gt", 0)),
        body=(
            "Before costs, this trade made {gross_pnl:.2f}. After ${total_friction:.2f} "
            "in slippage, spread, and fees, it closed at a net loss of {net_pnl:.2f}. The "
            "market call was right; the trade still lost, purely on cost structure — that "
            "distinction matters when judging whether the entry itself was sound."
        ),
    ),
    Tip(
        id="costs.target_hit_still_lost",
        category="costs",
        title="Hitting your target still lost money after costs",
        conditions=(_c("exit_reason", "eq", "target"), _c("net_pnl", "lte", 0)),
        body=(
            "The target price was reached, but the trade closed at {net_pnl:.2f} net. A "
            "target that doesn't clear friction isn't a real target — it needs to sit far "
            "enough away to survive the round-trip cost of getting in and out."
        ),
    ),
    Tip(
        id="costs.scalp_margin_thin",
        category="costs",
        title="Friction consumed most of this trade's gross gain",
        conditions=(
            _c("hold_minutes", "lt", 5),
            _c("friction_pct_of_gross_trade", "gt", 0.4),
        ),
        body=(
            "This trade was held under 5 minutes and friction took more than 40% of its "
            "gross gain. Very short holds need a proportionally larger price move to be "
            "worth the fixed cost of entering and exiting at all."
        ),
    ),
    # ---- execution -----------------------------------------------------
    Tip(
        id="execution.flattened_at_close",
        category="execution",
        title="This position was closed by the session, not by your plan",
        conditions=(_c("exit_reason", "eq", "eod_flat"),),
        body=(
            "This trade exited because the session ended while it was still open, not "
            "because a stop, target, or rule fired. The outcome ({net_pnl:.2f}) reflects "
            "wherever the price happened to be at the close, which is a different kind of "
            "evidence than a planned exit — weigh it accordingly."
        ),
    ),
    Tip(
        id="execution.risk_halted",
        category="execution",
        title="The risk engine closed this position, not you",
        conditions=(_c("exit_reason", "eq", "risk_halt"),),
        body=(
            "This exit was triggered by a risk control, not a discretionary or rule-based "
            "decision. That's the system doing its job — the outcome ({net_pnl:.2f}) is "
            "evidence about the risk halt's timing, not about the entry thesis."
        ),
    ),
    Tip(
        id="execution.long_hold_little_movement",
        category="execution",
        title="This position sat open a long time for very little movement",
        conditions=(
            _c("hold_minutes", "gte", 120),
            _c("r_multiple", "gt", -0.2),
            _c("r_multiple", "lt", 0.2),
        ),
        body=(
            "This trade was held for {hold_minutes:.0f} minutes and finished near "
            "breakeven ({r_multiple:.2f}R). Capital and attention sat tied up for hours "
            "for a result a coin flip could have produced faster — screen time has a cost "
            "even when the P&L looks neutral."
        ),
    ),
    Tip(
        id="execution.manual_exit_before_stop_or_target",
        category="execution",
        title="This was closed manually before its stop or target",
        conditions=(_c("exit_reason", "eq", "manual"), _c("net_pnl", "lt", 0)),
        body=(
            "This losing trade was closed manually rather than by the recorded stop. If "
            "the manual exit came in ahead of the stop, the realized loss should be "
            "smaller than the planned risk — worth confirming {net_pnl:.2f} actually "
            "reflects that, since an inconsistent pattern here is easy to miss."
        ),
    ),
    # ---- strategy --------------------------------------------------------
    Tip(
        id="strategy.no_evidence_recorded",
        category="strategy",
        title="No strategy evidence exists for this trade",
        conditions=(_c("has_signal_evidence", "eq", False),),
        body=(
            "This entry and exit were placed manually — there is no SignalRecord behind "
            "either leg, so there's no rule to check this outcome against. That's not a "
            "flaw in this trade; it just means it can only be judged on process (was "
            "there a plan and a stop) and outcome, not on whether a strategy's conditions "
            "were met, because none were evaluated."
        ),
    ),
    Tip(
        id="strategy.thesis_confirmed",
        category="strategy",
        title="This trade played out the way its entry rule expected",
        conditions=(_c("exit_reason", "eq", "target"), _c("followed_rules", "eq", True)),
        body=(
            "The entry conditions were met, and the trade reached its target as designed. "
            "One confirming trade doesn't prove the rule has an edge — expectancy is "
            "measured over many trades — but it's a clean data point in the sample."
        ),
    ),
    Tip(
        id="strategy.rule_followed_still_lost",
        category="strategy",
        title="The entry rule was followed correctly and still lost",
        conditions=(
            _c("exit_reason", "eq", "stop"),
            _c("followed_rules", "eq", True),
            _c("net_pnl", "lt", 0),
        ),
        body=(
            "Every recorded condition for this entry was satisfied, and the trade still "
            "lost {net_pnl:.2f}. A single loss on a correctly-followed rule says nothing "
            "about whether the rule has an edge — that question only has an answer over "
            "a large enough sample of trades, win or lose."
        ),
    ),
)

# Rotating fallback pool — "fall back to a rotating fundamentals tip if
# nothing matches" (§11.3). These never trigger-match (no conditions); the
# rotation itself is index-based on a caller-supplied sequence number so the
# same trade always maps to the same fundamentals tip (deterministic).
FUNDAMENTALS_TIPS: tuple[Tip, ...] = (
    Tip(
        id="fundamentals.expectancy_over_single_trades",
        category="fundamentals",
        title="One trade is not a verdict",
        body=(
            "No single trade, win or lose, tells you whether a strategy works. "
            "Expectancy — average result per trade over a large enough sample — is the "
            "only number that answers that question. Keep recording the process, not "
            "just the outcome."
        ),
    ),
    Tip(
        id="fundamentals.friction_is_real",
        category="fundamentals",
        title="Friction is a cost on every trade, win or lose",
        body=(
            "Slippage, spread, and commissions apply the same way to a winning trade as "
            "a losing one. A strategy's edge has to clear that cost on average, not just "
            "look good on the trades where it didn't matter."
        ),
    ),
    Tip(
        id="fundamentals.stop_defines_the_trade",
        category="fundamentals",
        title="The stop is what makes a trade a trade",
        body=(
            "A defined stop is what turns a guess into a sized, risk-limited position. "
            "Every entry in this app requires one for that reason — it's the difference "
            "between a known, bounded loss and an open-ended one."
        ),
    ),
    Tip(
        id="fundamentals.benchmark_the_alternative",
        category="fundamentals",
        title="Compare against doing nothing",
        body=(
            "The honest question for any trading process is whether it beats simply "
            "holding the benchmark over the same period. Check the Reality Ledger "
            "regularly — it's the one number that can't be flattered by cherry-picked "
            "trades."
        ),
    ),
    Tip(
        id="fundamentals.process_over_outcome",
        category="fundamentals",
        title="Judge the decision, not just the result",
        body=(
            "A good decision can lose money, and a bad one can win, over any single "
            "trade. Recording the reasoning at the time — not after the outcome is known "
            "— is the only way to judge decision quality honestly."
        ),
    ),
)


def select_tip(
    context: Mapping[str, Any],
    recent_tip_ids: Sequence[str] = (),
    consecutive_trigger_counts: Mapping[str, int] | None = None,
    fundamentals_rotation_index: int = 0,
) -> Tip:
    """Pick one tip for a trade's context. Deterministic: the same
    `context` (plus the same suppression history) always yields the same
    tip -- required by the Phase 5 acceptance criteria.

    `recent_tip_ids`: the tip ids shown on this account's last
    `SUPPRESSION_WINDOW` trades, most-recent-first.
    `consecutive_trigger_counts`: for each tip id, how many of the trades
    immediately preceding this one (consecutively, not counting this one)
    also matched that tip's trigger rule — see this module's docstring for
    why this is tracked separately from `recent_tip_ids`.
    `fundamentals_rotation_index`: a monotonic sequence number (e.g. the
    account's total trade count) used to rotate the fundamentals fallback
    deterministically rather than randomly.
    """
    counts = consecutive_trigger_counts or {}

    matches = [tip for tip in TIPS if tip.matches(context)]
    matches.sort(key=lambda t: CATEGORY_PRIORITY.get(t.category, 99))

    for tip in matches:
        shown_recently = tip.id in recent_tip_ids
        if not shown_recently:
            return tip
        # Shown recently -- suppressed unless this is the 3rd (or later)
        # consecutive trigger, in which case repetition is itself the signal.
        streak_including_now = counts.get(tip.id, 0) + 1
        if streak_including_now >= SUPPRESSION_OVERRIDE_STREAK:
            return tip

    if not FUNDAMENTALS_TIPS:
        raise RuntimeError("no fundamentals tips configured")  # pragma: no cover
    return FUNDAMENTALS_TIPS[fundamentals_rotation_index % len(FUNDAMENTALS_TIPS)]
