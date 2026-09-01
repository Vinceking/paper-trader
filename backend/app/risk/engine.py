"""The risk engine. BUILD_SPEC §7.4.

Runs after signal creation, before order submission — for Phase 2, after a
manual order request is formed, before it reaches PaperBroker. Every veto
here is meant to surface in the journal as a teachable moment later
(BUILD_SPEC: "vetoes are some of the most valuable content in the app"), so
each one carries a `detail` dict a caller can persist verbatim into
`risk_events.detail`.

Vetoes only ever apply to entries. An exit is always approved — trapping
someone in a losing position because a risk rail fired is exactly backwards;
the whole point of these rails is to bound damage, not add to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.risk.sizing import clamp_to_max_position, fixed_fractional_qty

Side = Literal["buy", "sell"]
Intent = Literal["entry", "exit"]


@dataclass(frozen=True)
class RiskSignal:
    """The generic order intent the engine evaluates.

    Not the full BUILD_SPEC §8.1 `Signal` (that's Phase 3's strategy output)
    — Phase 2 only has manual orders, so this carries what a manual order and
    a future strategy Signal share: symbol, side, intent, and a stop.
    """

    symbol: str
    side: Side
    intent: Intent
    entry_price: Decimal  # reference price sizing is computed against
    stop_price: Decimal | None  # REQUIRED for entries — CLAUDE.md rule 6


@dataclass(frozen=True)
class RiskSettingsInput:
    risk_per_trade_pct: Decimal
    max_daily_loss_pct: Decimal
    max_open_positions: int
    max_trades_per_day: int
    max_position_pct: Decimal
    cooldown_after_losses: int
    cooldown_minutes: int


@dataclass(frozen=True)
class AccountState:
    """Everything the engine needs to evaluate vetoes.

    Assembled by the caller from the database — the engine itself never
    queries anything, which is what keeps it unit-testable without one.
    """

    equity: Decimal
    starting_equity_today: Decimal
    realized_pnl_today: Decimal
    open_positions_count: int
    trades_today_count: int
    consecutive_losses: int
    last_loss_at: datetime | None
    now: datetime
    # Minutes until the session closes; None if unknown/market closed (the
    # near-close veto only fires when this is a known, small, positive value).
    minutes_until_close: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: Decimal | None
    veto_reason: str | None
    detail: dict = field(default_factory=dict)


class RiskEngine:
    def evaluate(
        self,
        signal: RiskSignal,
        account: AccountState,
        settings: RiskSettingsInput,
    ) -> RiskDecision:
        if signal.intent == "exit":
            return RiskDecision(approved=True, qty=None, veto_reason=None)

        veto = self._check_vetoes(account, settings)
        if veto is not None:
            reason, detail = veto
            return RiskDecision(approved=False, qty=None, veto_reason=reason, detail=detail)

        if signal.stop_price is None:
            return RiskDecision(
                approved=False, qty=None, veto_reason="missing_stop",
                detail={"symbol": signal.symbol, "reason": "entry has no stop_price"},
            )

        qty = fixed_fractional_qty(
            account.equity, settings.risk_per_trade_pct, signal.entry_price, signal.stop_price,
        )
        if qty <= 0:
            return RiskDecision(
                approved=False, qty=None, veto_reason="size_zero",
                detail={
                    "symbol": signal.symbol,
                    "entry_price": str(signal.entry_price),
                    "stop_price": str(signal.stop_price),
                },
            )

        qty = clamp_to_max_position(
            qty, signal.entry_price, account.equity, settings.max_position_pct,
        )
        if qty <= 0:
            return RiskDecision(
                approved=False, qty=None, veto_reason="size_zero",
                detail={"symbol": signal.symbol, "reason": "clamped to zero by max_position_pct"},
            )

        return RiskDecision(approved=True, qty=qty, veto_reason=None)

    def _check_vetoes(
        self, account: AccountState, settings: RiskSettingsInput,
    ) -> tuple[str, dict] | None:
        if account.starting_equity_today > 0:
            loss_pct = max(Decimal(0), -account.realized_pnl_today) / account.starting_equity_today
            if loss_pct >= settings.max_daily_loss_pct:
                return "daily_halt", {
                    "realized_pnl_today": str(account.realized_pnl_today),
                    "loss_pct": str(loss_pct),
                    "max_daily_loss_pct": str(settings.max_daily_loss_pct),
                }

        in_loss_streak = account.consecutive_losses >= settings.cooldown_after_losses
        if in_loss_streak and account.last_loss_at is not None:
            minutes_since_loss = (account.now - account.last_loss_at).total_seconds() / 60
            if minutes_since_loss < settings.cooldown_minutes:
                return "cooldown", {
                    "consecutive_losses": account.consecutive_losses,
                    "minutes_since_last_loss": round(minutes_since_loss, 2),
                    "cooldown_minutes": settings.cooldown_minutes,
                }

        if account.open_positions_count >= settings.max_open_positions:
            return "max_positions", {
                "open_positions_count": account.open_positions_count,
                "max_open_positions": settings.max_open_positions,
            }

        if account.trades_today_count >= settings.max_trades_per_day:
            return "max_trades", {
                "trades_today_count": account.trades_today_count,
                "max_trades_per_day": settings.max_trades_per_day,
            }

        if account.minutes_until_close is not None and account.minutes_until_close <= 10:
            return "near_close", {"minutes_until_close": account.minutes_until_close}

        return None
