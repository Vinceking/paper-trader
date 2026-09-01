"""User and paper account tables. BUILD_SPEC §5."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, uuid_pk


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'requester' | 'approver' — see ADDENDUM_LIVE_APPROVAL §3.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="requester")
    created_at: Mapped[datetime] = created_at_col()


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    starting_cash: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("100000.00")
    )
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    # Reality Ledger anchor — BUILD_SPEC §12.
    benchmark_symbol: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SPY"
    )
    benchmark_start_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    created_at: Mapped[datetime] = created_at_col()
