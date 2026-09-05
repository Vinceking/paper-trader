"""Account/positions/trades read-path API schemas. BUILD_SPEC §14."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class AccountOut(BaseModel):
    id: UUID
    name: str
    cash: Decimal
    equity: Decimal
    starting_cash: Decimal
    benchmark_symbol: str


class PositionOut(BaseModel):
    id: UUID
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    stop_price: Decimal | None
    target_price: Decimal | None
    opened_at: datetime
    status: str


class TradeOut(BaseModel):
    id: UUID
    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_pnl: Decimal
    total_friction: Decimal
    net_pnl: Decimal
    r_multiple: Decimal | None
    exit_reason: str
