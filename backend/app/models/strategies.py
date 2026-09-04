"""The `strategies` table. BUILD_SPEC §5, §8.5.

Named `StrategyRecord` (not `Strategy`) to avoid colliding with the domain
`Strategy` ABC in app/strategies/base.py — same table, same meaning, just a
distinct Python name since both are in scope together wherever a signal
gets persisted against its owning strategy.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

_JSONB_PORTABLE = JSONB().with_variant(JSON(), "sqlite")


class StrategyRecord(Base):
    __tablename__ = "strategies"
    __table_args__ = (UniqueConstraint("user_id", "slug", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)  # 'orb', 'vwap_reversion', ...
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    params: Mapped[dict] = mapped_column(_JSONB_PORTABLE, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Backtest gate (§8.5) — Phase 4. enabled cannot be set true unless this is.
    gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gate_report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
