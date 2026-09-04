"""Order and fill tables. BUILD_SPEC §5, §7.5, §9."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("paper_accounts.id"), nullable=False
    )
    # Null for manual orders. Phase 3 adds the signals table this references
    # but doesn't populate it — auto-executing a strategy signal (risk engine
    # -> broker) is a later integration, not part of Phase 3's own scope.
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("signals.id")
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # 'buy' | 'sell'
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False, default="market")
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    # 'pending' | 'filled' | 'partial' | 'cancelled' | 'rejected'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 'strategy' | 'manual' — only 'manual' is produced through Phase 3.
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(64))


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # Friction breakdown, itemized so the UI can teach cost awareness. §9.3.
    reference_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    slippage_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    spread_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    reg_fees: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
