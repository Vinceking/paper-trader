"""Manual order API schemas. BUILD_SPEC §14."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ReferenceQuoteIn(BaseModel):
    bid: Decimal
    ask: Decimal
    atr: Decimal
    typical_bar_volume: Decimal


class ManualOrderIn(BaseModel):
    account_id: UUID
    symbol: str = Field(min_length=1, max_length=16)
    side: Literal["buy", "sell"]
    intent: Literal["entry", "exit"]
    quote: ReferenceQuoteIn
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    # Proof the frontend's 3-second hold-to-confirm (§13.4) actually ran.
    # Phase 2 only checks that it's present; the hold itself is enforced
    # client-side.
    confirm_token: str = Field(min_length=1)


class FillOut(BaseModel):
    qty: Decimal
    reference_price: Decimal
    fill_price: Decimal
    slippage_cost: Decimal
    spread_cost: Decimal
    commission: Decimal
    reg_fees: Decimal


class TradeOut(BaseModel):
    id: UUID
    gross_pnl: Decimal
    total_friction: Decimal
    net_pnl: Decimal
    r_multiple: Decimal | None
    exit_reason: str


class ManualOrderOut(BaseModel):
    order_id: UUID
    status: str
    fill: FillOut | None = None
    position_id: UUID | None = None
    trade: TradeOut | None = None
