"""Position and trade tables. BUILD_SPEC §5.

A `Position` is an open round trip; closing it writes a `Trade` row.

Deviation from BUILD_SPEC §5: `Position.entry_order_id` is not in the spec's
schema. Computing a trade's `total_friction` on close requires the entry
leg's itemized friction as well as the exit leg's, and the spec's schema has
no way to trace a position back to the order that opened it (no
`positions.entry_order_id`, and `entry_signal_id` doesn't exist until Phase 3
signals do). Added here so that lookup is possible; everything else matches §5 exactly.

`strategy_id`/`entry_signal_id`/`exit_signal_id` are nullable and, for now,
always null — nothing in Phase 3 auto-executes a strategy signal into a real
position/trade yet (see the note on `Order.signal_id`). They exist so the
schema matches §5 and a future integration doesn't need another migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("paper_accounts.id"), nullable=False
    )
    # Not in BUILD_SPEC §5 — see module docstring.
    entry_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategies.id")
    )
    entry_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("signals.id")
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")


class Trade(Base):
    """A completed round trip. One row per closed trade."""

    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("paper_accounts.id"), nullable=False
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategies.id")
    )
    entry_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("signals.id")
    )
    exit_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("signals.id")
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    total_friction: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    # 'stop' | 'target' | 'signal' | 'manual' | 'eod_flat' | 'risk_halt'
    exit_reason: Mapped[str] = mapped_column(String(16), nullable=False)

    # Reality Ledger anchor (§12) — populated once the benchmark service exists.
    benchmark_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
