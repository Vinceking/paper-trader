"""The explainer. BUILD_SPEC §11.1/§11.2, Phase 5.

Turns a closed `Trade` row (plus its linked `SignalRecord`s, when they
exist) into the four §11.2 output sections and a selected §11.3 tip, and
persists both as one `ExplanationRecord`. This module is the only place
that assembles the "deterministic input JSON" §11.2 describes, and the only
place that decides whether to attempt the LLM path before falling back to
the guaranteed deterministic template.

### The cardinal rule (CLAUDE.md rule 2 / BUILD_SPEC §11.1)

This module never receives raw price history and never infers a cause. Every
value in the assembled payload traces to a real column on `Trade` or a real
entry in a `SignalRecord`'s `features`/`conditions`. Where no `SignalRecord`
is linked — which is every trade tonight, see the module docstring in
`app/models/positions.py` — the entry/exit sections say so plainly instead
of inventing a rule_id or conditions that were never evaluated.

### Why generation is synchronous here, not in a worker (documented deviation)

BUILD_SPEC §11.2 says "generate async in the worker; push `explanation_ready`
over the WebSocket." CLAUDE.md's stack reference lists three processes —
`api`, `ingest`, `worker` — but no `worker` process exists anywhere in this
codebase tonight; nothing consumes a queue. Given that, and given the
deterministic template path is instant and the LLM path isn't reachable
tonight anyway (no API key), generating synchronously right after the
trade's own commit is a documented simplification, not a silent scope cut —
see `app/execution/order_service.py` for the call site.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.education.llm import Explanation, LLMExplanationError, LLMProvider
from app.education.tips import Tip, select_tip
from app.market_calendar import start_of_trading_day
from app.models.explanations import ExplanationRecord
from app.models.positions import Trade
from app.models.signals import SignalRecord

log = structlog.get_logger(__name__)

# How many immediately-preceding trades to reconstruct context for when
# checking a tip's "triggers three times in a row" override (§11.3). Capped
# at 2 deliberately -- see app/education/tips.py's module docstring: the
# override threshold is exactly 3, so knowing "at least 2 prior consecutive
# matches" is all a correct decision ever needs.
_STREAK_LOOKBACK = 2
_SUPPRESSION_HISTORY_LIMIT = 10  # mirrors tips.SUPPRESSION_WINDOW


class ExplanationAlreadyExistsError(Exception):
    """Not raised in normal operation -- `generate_explanation_for_trade`
    checks for an existing row and returns it instead. Reserved for the
    unique-constraint race (two concurrent close requests for the same
    trade), which this single-process synchronous design doesn't otherwise
    produce, but the DB constraint (see app/models/explanations.py) is the
    actual backstop either way."""


def _to_float(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


def _aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC.

    SQLite (the test database -- see tests/conftest.py) doesn't preserve
    `tzinfo` through a round trip even on a `DateTime(timezone=True)`
    column, so a `Trade` row re-loaded after a commit can come back naive
    even though every datetime this app ever writes is UTC (see
    `app/deps.py`'s `get_now`). Real Postgres preserves it; this is a
    test-database quirk, normalized here rather than left to raise on
    arithmetic between a naive and an aware datetime.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Deterministic context assembly — shared by the live explanation and by the
# tip suppression streak lookback (see app/education/tips.py's docstring).
# ---------------------------------------------------------------------------


async def _load_signals(
    db: AsyncSession, trade: Trade
) -> tuple[SignalRecord | None, SignalRecord | None]:
    entry_signal = (
        await db.get(SignalRecord, trade.entry_signal_id) if trade.entry_signal_id else None
    )
    exit_signal = (
        await db.get(SignalRecord, trade.exit_signal_id) if trade.exit_signal_id else None
    )
    return entry_signal, exit_signal


async def _trades_today(db: AsyncSession, trade: Trade) -> list[Trade]:
    day_start = start_of_trading_day(_aware(trade.closed_at))
    rows = (
        await db.execute(
            select(Trade)
            .where(
                Trade.account_id == trade.account_id,
                Trade.closed_at >= day_start,
                Trade.closed_at <= trade.closed_at,
            )
            .order_by(Trade.closed_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def _trades_before(db: AsyncSession, trade: Trade, limit: int) -> list[Trade]:
    """The `limit` trades for this account that closed most recently before
    `trade` (most-recent-first)."""
    rows = (
        await db.execute(
            select(Trade)
            .where(Trade.account_id == trade.account_id, Trade.closed_at < trade.closed_at)
            .order_by(Trade.closed_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def build_trade_context(db: AsyncSession, trade: Trade) -> dict[str, Any]:
    """The tip-selector's trade context: plain scalars, all traceable to a
    real `trades`/`signals` row. Works for any closed trade, including
    reconstructing a *past* trade's context for the suppression-streak
    lookback -- every aggregate below is computed relative to `trade`'s own
    `closed_at`, using only trades that had already closed by then.
    """
    entry_signal, exit_signal = await _load_signals(db, trade)
    has_signal_evidence = entry_signal is not None or exit_signal is not None

    followed_rules: bool | None = None
    if entry_signal is not None:
        followed_rules = all(c.get("passed") for c in entry_signal.conditions)
        if exit_signal is not None:
            followed_rules = followed_rules and all(
                c.get("passed") for c in exit_signal.conditions
            )

    hold_minutes = (_aware(trade.closed_at) - _aware(trade.opened_at)).total_seconds() / 60.0

    todays_trades = await _trades_today(db, trade)
    trades_today = len(todays_trades)
    gross_abs_sum = sum((abs(t.gross_pnl) for t in todays_trades), Decimal(0))
    friction_sum = sum((t.total_friction for t in todays_trades), Decimal(0))
    friction_pct_of_gross_today = (
        float(friction_sum / gross_abs_sum) if gross_abs_sum > 0 else None
    )

    prior_trades = await _trades_before(db, trade, limit=20)  # most-recent-first
    consecutive_losses = 0
    for t in prior_trades:
        if t.net_pnl >= 0:
            break
        consecutive_losses += 1

    seconds_since_last_exit = None
    if prior_trades:
        seconds_since_last_exit = (
            _aware(trade.opened_at) - _aware(prior_trades[0].closed_at)
        ).total_seconds()

    friction_pct_of_gross_trade = (
        float(trade.total_friction / trade.gross_pnl) if trade.gross_pnl > 0 else None
    )

    return {
        "exit_reason": trade.exit_reason,
        "net_pnl": _to_float(trade.net_pnl),
        "gross_pnl": _to_float(trade.gross_pnl),
        "total_friction": _to_float(trade.total_friction),
        "r_multiple": _to_float(trade.r_multiple),
        "hold_minutes": hold_minutes,
        "source": "strategy" if entry_signal is not None else "manual",
        "has_signal_evidence": has_signal_evidence,
        "followed_rules": followed_rules,
        "trades_today": trades_today,
        "friction_pct_of_gross_today": friction_pct_of_gross_today,
        "friction_pct_of_gross_trade": friction_pct_of_gross_trade,
        "consecutive_losses": consecutive_losses,
        "seconds_since_last_exit": seconds_since_last_exit,
        # Not derivable from anything recorded in this build tonight -- see
        # app/education/tips.py's module docstring. Left present-but-None
        # rather than omitted, so tips that reference them fail closed
        # instead of raising a KeyError.
        "mfe_r": None,
        "stop_distance_atr": None,
    }


async def _consecutive_trigger_counts(
    db: AsyncSession, trade: Trade, candidate_tips: list[Tip]
) -> dict[str, int]:
    """For each candidate tip, how many of the trades immediately preceding
    `trade` (consecutively, most-recent-first, capped at `_STREAK_LOOKBACK`)
    also matched that tip's trigger rule. See app/education/tips.py's
    docstring for why this can't just be read off `recent_tip_ids`."""
    prior = await _trades_before(db, trade, limit=_STREAK_LOOKBACK)
    counts: dict[str, int] = {}
    for tip in candidate_tips:
        streak = 0
        for prior_trade in prior:  # most-recent-first == walking the streak backward
            prior_context = await build_trade_context(db, prior_trade)
            if not tip.matches(prior_context):
                break
            streak += 1
        counts[tip.id] = streak
    return counts


async def _recent_tip_ids(db: AsyncSession, trade: Trade) -> list[str]:
    rows = (
        await db.execute(
            select(ExplanationRecord.tip_id)
            .join(Trade, Trade.id == ExplanationRecord.trade_id)
            .where(Trade.account_id == trade.account_id, Trade.closed_at < trade.closed_at)
            .order_by(Trade.closed_at.desc())
            .limit(_SUPPRESSION_HISTORY_LIMIT)
        )
    ).scalars().all()
    return [tip_id for tip_id in rows if tip_id is not None]


async def _account_trade_count(db: AsyncSession, trade: Trade) -> int:
    rows = (
        await db.execute(
            select(Trade.id).where(
                Trade.account_id == trade.account_id, Trade.closed_at <= trade.closed_at
            )
        )
    ).scalars().all()
    return len(rows)


async def choose_tip(db: AsyncSession, trade: Trade, context: dict[str, Any]) -> Tip:
    from app.education.tips import TIPS  # local import avoids a module-level cycle risk

    candidates = [tip for tip in TIPS if tip.matches(context)]
    recent_ids = await _recent_tip_ids(db, trade)
    streak_counts = await _consecutive_trigger_counts(db, trade, candidates)
    rotation_index = await _account_trade_count(db, trade)
    return select_tip(
        context,
        recent_tip_ids=recent_ids,
        consecutive_trigger_counts=streak_counts,
        fundamentals_rotation_index=rotation_index,
    )


# ---------------------------------------------------------------------------
# LLM payload assembly — BUILD_SPEC §11.2's exact shape.
# ---------------------------------------------------------------------------


def assemble_payload(
    trade: Trade,
    entry_signal: SignalRecord | None,
    exit_signal: SignalRecord | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    """The deterministic input JSON, §11.2's shape. Every value here comes
    directly from a `trades`/`signals` column -- nothing is computed by
    asking a model, and nothing here is raw price history."""

    def _signal_section(signal: SignalRecord | None) -> dict[str, Any]:
        if signal is None:
            return {"manual": True}
        return {
            "manual": False,
            "rule_id": signal.rule_id,
            "rule_text": signal.rule_text,
            "conditions": list(signal.conditions),
            "features": dict(signal.features),
            "confidence": _to_float(signal.confidence),
        }

    entry_extra = {
        "entry_price": _to_float(trade.entry_price),
        "side": trade.side,
        "qty": _to_float(trade.qty),
    }
    exit_extra = {"exit_price": _to_float(trade.exit_price), "exit_reason": trade.exit_reason}
    return {
        "entry": _signal_section(entry_signal) | entry_extra,
        "exit": _signal_section(exit_signal) | exit_extra,
        "outcome": {
            "gross_pnl": _to_float(trade.gross_pnl),
            "friction": _to_float(trade.total_friction),
            "net_pnl": _to_float(trade.net_pnl),
            "r_multiple": _to_float(trade.r_multiple),
            "hold_minutes": context["hold_minutes"],
        },
        "context": {
            "trades_today": context["trades_today"],
            "consecutive_losses": context["consecutive_losses"],
            "friction_pct_of_gross_today": context["friction_pct_of_gross_today"],
        },
    }


def _prompt_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The guaranteed path: deterministic template renderer. Zero dependencies,
# zero network calls. BUILD_SPEC §11.2: "The app must never silently drop
# the explanation -- the explanation *is* the product."
# ---------------------------------------------------------------------------


def _fmt_money(value: float | None) -> str:
    return "an unrecorded amount" if value is None else f"${value:,.2f}"


def render_template(
    trade: Trade,
    entry_signal: SignalRecord | None,
    exit_signal: SignalRecord | None,
    context: dict[str, Any],
) -> Explanation:
    entry_rationale = _render_entry_rationale(trade, entry_signal)
    exit_rationale = _render_exit_rationale(trade, exit_signal)
    what_went_right, what_went_wrong = _render_outcome(trade, context)
    return Explanation(
        entry_rationale=entry_rationale,
        exit_rationale=exit_rationale,
        what_went_right=what_went_right,
        what_went_wrong=what_went_wrong,
    )


def _render_entry_rationale(trade: Trade, entry_signal: SignalRecord | None) -> str:
    if entry_signal is None:
        return (
            f"This was a manual entry with no recorded strategy evidence: no rule fired, "
            f"and no conditions were evaluated. You bought {trade.qty} shares of "
            f"{trade.symbol} at ${trade.entry_price:.2f}. There is no signal-level "
            f"evidence to cite here — only the trade record itself."
        )
    passed = [c for c in entry_signal.conditions if c.get("passed")]
    failed = [c for c in entry_signal.conditions if not c.get("passed")]
    parts = [
        f"Entry rule {entry_signal.rule_id!r} fired: {entry_signal.rule_text}",
        f"Entered {trade.qty} shares of {trade.symbol} at ${trade.entry_price:.2f}.",
    ]
    if passed:
        met = "; ".join(
            f"{c.get('name')} {c.get('operator')} {c.get('threshold')} "
            f"(actual {c.get('actual')})"
            for c in passed
        )
        parts.append(f"Conditions met: {met}.")
    if failed:
        unmet = "; ".join(f"{c.get('name')} (actual {c.get('actual')})" for c in failed)
        parts.append(f"Recorded but not required to pass: {unmet}.")
    return " ".join(parts)


def _render_exit_rationale(trade: Trade, exit_signal: SignalRecord | None) -> str:
    reason_text = {
        "stop": "the stop price was hit",
        "target": "the target price was reached",
        "signal": "a strategy exit rule fired",
        "manual": "you closed it manually",
        "eod_flat": "the session ended while it was still open",
        "risk_halt": "the risk engine forced it closed",
    }.get(trade.exit_reason, trade.exit_reason)

    base = (
        f"This position closed because {reason_text}, at ${trade.exit_price:.2f} "
        f"(exit_reason={trade.exit_reason!r})."
    )
    if exit_signal is None:
        return base + (
            " No SignalRecord is linked to this exit — the reason above is the trade "
            "record's own field, not a strategy's evaluated rule."
        )
    met = "; ".join(
        f"{c.get('name')} {c.get('operator')} {c.get('threshold')} (actual {c.get('actual')})"
        for c in exit_signal.conditions
        if c.get("passed")
    )
    return base + f" Exit rule {exit_signal.rule_id!r}: {exit_signal.rule_text}." + (
        f" Conditions met: {met}." if met else ""
    )


def _render_outcome(trade: Trade, context: dict[str, Any]) -> tuple[str, str]:
    net = float(trade.net_pnl)
    gross = float(trade.gross_pnl)
    friction = float(trade.total_friction)
    r = context["r_multiple"]
    followed_rules = context["followed_rules"]
    hold_minutes = context["hold_minutes"]

    facts = (
        f"Gross P&L was {_fmt_money(gross)}, friction was {_fmt_money(friction)}, "
        f"net P&L was {_fmt_money(net)}"
        + (f", an {r:.2f}R result" if r is not None else "")
        + f", held {hold_minutes:.1f} minutes."
    )

    if net > 0 and followed_rules is False:
        wrong = (
            f"This was a win, but the recorded entry conditions were not satisfied when "
            f"it was taken — {facts} A win that broke the process is not evidence the "
            f"process works."
        )
        right = (
            "Nothing here supports calling this a good decision — the money made does "
            "not offset that the rule wasn't followed."
        )
        return right, wrong

    if net < 0 and followed_rules is True:
        right = (
            f"This was a good trade. It lost money, and it lost the amount the plan "
            f"called for, for the reason recorded at entry. {facts} Separating decision "
            f"quality from outcome is the point of tracking this at all."
        )
        wrong = "The data does not support calling this a mistake — the recorded rule was followed."
        return right, wrong

    if net >= 0:
        right = f"This trade closed positive. {facts}"
        wrong = "No loss to account for here."
        if friction > 0 and gross > 0:
            wrong = (
                f"Friction of {_fmt_money(friction)} reduced the gross gain of "
                f"{_fmt_money(gross)} to a net of {_fmt_money(net)} — that cost is real "
                f"and recurs on every trade."
            )
        return right, wrong

    right = "No gain to account for here."
    wrong = (
        f"This trade closed negative. {facts} The data above is the full extent of "
        f"what's recorded about why."
    )
    return right, wrong


# ---------------------------------------------------------------------------
# Orchestration: try the LLM (if configured), fall back to the template,
# persist exactly one ExplanationRecord per trade.
# ---------------------------------------------------------------------------


async def generate_explanation_for_trade(
    db: AsyncSession,
    trade: Trade,
    llm_provider: LLMProvider | None,
) -> ExplanationRecord:
    """Generate and persist the explanation for a just-closed trade.
    Idempotent: if a trade already has an ExplanationRecord, that row is
    returned unchanged rather than generating (and inserting) a second one.
    """
    existing = (
        await db.execute(select(ExplanationRecord).where(ExplanationRecord.trade_id == trade.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    entry_signal, exit_signal = await _load_signals(db, trade)
    context = await build_trade_context(db, trade)
    payload = assemble_payload(trade, entry_signal, exit_signal, context)

    source = "template"
    prompt_hash: str | None = None
    sections: Explanation | None = None

    if llm_provider is not None:
        candidate_hash = _prompt_hash(payload)
        try:
            sections = await llm_provider.explain(payload)
            source = "llm"
            prompt_hash = candidate_hash
        except LLMExplanationError as exc:
            log.warning(
                "explanation.llm_failed_falling_back", trade_id=str(trade.id), error=str(exc)
            )
            sections = None

    if sections is None:
        sections = render_template(trade, entry_signal, exit_signal, context)
        source = "template"
        prompt_hash = None

    tip = await choose_tip(db, trade, context)

    record = ExplanationRecord(
        trade_id=trade.id,
        entry_rationale=sections.entry_rationale,
        exit_rationale=sections.exit_rationale,
        what_went_right=sections.what_went_right,
        what_went_wrong=sections.what_went_wrong,
        tip_id=tip.id,
        tip_body=tip.render(context),
        source=source,
        prompt_hash=prompt_hash,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record
