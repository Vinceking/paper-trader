"""The `signals` table — the evidence record. BUILD_SPEC §5, §11.1.

The heart of the education layer: written *before* any order is submitted,
with the exact rule, every condition (including the ones that failed), and
the complete feature snapshot. CLAUDE.md rule 2. Named `SignalRecord` (not
`Signal`) to avoid colliding with the domain `Signal` dataclass in
app/strategies/base.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

_JSONB_PORTABLE = JSONB().with_variant(JSON(), "sqlite")


class SignalRecord(Base):
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategies.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("paper_accounts.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # 'buy' | 'sell'
    intent: Mapped[str] = mapped_column(String(8), nullable=False)  # 'entry' | 'exit'
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 'orb.breakout_long'
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Every input the strategy actually looked at, at this instant.
    features: Mapped[dict] = mapped_column(_JSONB_PORTABLE, nullable=False)
    # Which conditions evaluated true/false and their thresholds. Always the
    # FULL list, including failures — CLAUDE.md / BUILD_SPEC §8.2.
    conditions: Mapped[list] = mapped_column(_JSONB_PORTABLE, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    acted_on: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    veto_reason: Mapped[str | None] = mapped_column(Text)
