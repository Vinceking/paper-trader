"""Risk settings and risk event tables. BUILD_SPEC §5, §7.4."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

# JSONB on Postgres (matches BUILD_SPEC §5 exactly), plain JSON on SQLite so
# the same model runs in tests without a Postgres/Docker dependency.
_JSONB_PORTABLE = JSONB().with_variant(JSON(), "sqlite")


class RiskSettings(Base):
    __tablename__ = "risk_settings"

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("paper_accounts.id"), primary_key=True
    )
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=Decimal("0.01")
    )
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=Decimal("0.03")
    )
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_position_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=Decimal("0.20")
    )
    cooldown_after_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("paper_accounts.id"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 'veto' | 'daily_halt' | 'cooldown_start'
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict] = mapped_column(_JSONB_PORTABLE, nullable=False)
