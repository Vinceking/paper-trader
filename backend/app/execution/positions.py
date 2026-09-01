"""Position and trade lifecycle. BUILD_SPEC §5, Phase 2 acceptance criteria.

Pure and DB-free on purpose, matching the pattern in app/ingest/bars.py: the
caller (the /orders route in Phase 2) is responsible for loading position
state and persisting the resulting Trade row. `close_position` just needs
both legs' friction totals — the entry leg's is carried forward from when
the position was opened (`Position.entry_order_id` on the ORM model links
back to the entry Fill so the caller can fetch it).

Only long positions are exercised in this build: BUILD_SPEC §0.3 notes that
custodial/cash accounts get no margin, which means no shorting. The P&L math
below is direction-generic (it would also be correct for a short leg) simply
because getting the sign right is inherent to correct arithmetic, not
because shorting is a supported feature here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

Side = Literal["buy", "sell"]
# 'manual' covers Phase 2's only exit path; 'stop'/'target'/'signal'/'eod_flat'/
# 'risk_halt' are automatic exits that arrive with the Phase 3 strategy engine.
ExitReason = Literal["stop", "target", "signal", "manual", "eod_flat", "risk_halt"]


@dataclass(frozen=True)
class OpenPosition:
    """The position state close_position needs. Mirrors models.positions.Position."""

    symbol: str
    side: Side  # side of the entry order that opened it ('buy' = long)
    qty: Decimal
    avg_entry_price: Decimal
    stop_price: Decimal | None
    opened_at: datetime


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_pnl: Decimal
    total_friction: Decimal
    net_pnl: Decimal
    r_multiple: Decimal | None
    exit_reason: ExitReason


def close_position(
    position: OpenPosition,
    exit_price: Decimal,
    exit_qty: Decimal,
    entry_friction: Decimal,
    exit_friction: Decimal,
    closed_at: datetime,
    exit_reason: ExitReason,
) -> ClosedTrade:
    direction = Decimal(1) if position.side == "buy" else Decimal(-1)
    gross_pnl = (exit_price - position.avg_entry_price) * exit_qty * direction
    total_friction = entry_friction + exit_friction
    net_pnl = gross_pnl - total_friction

    r_multiple = None
    if position.stop_price is not None:
        initial_risk = abs(position.avg_entry_price - position.stop_price) * exit_qty
        if initial_risk > 0:
            r_multiple = net_pnl / initial_risk

    return ClosedTrade(
        symbol=position.symbol,
        side=position.side,
        qty=exit_qty,
        entry_price=position.avg_entry_price,
        exit_price=exit_price,
        opened_at=position.opened_at,
        closed_at=closed_at,
        gross_pnl=gross_pnl,
        total_friction=total_friction,
        net_pnl=net_pnl,
        r_multiple=r_multiple,
        exit_reason=exit_reason,
    )
