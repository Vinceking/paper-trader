"""The `gate_reports` table. BUILD_SPEC §8.5, Phase 4.

Deviation from BUILD_SPEC, flagged per CLAUDE.md's convention (same as
`Position.entry_order_id` elsewhere in this codebase): §5's literal SQL only
mentions `strategies.gate_report_id UUID` with no defined target table for
it to reference. This module defines that target: one row per backtest run,
storing the full per-criterion pass/fail detail BUILD_SPEC §8.5 and the
Phase 4 acceptance criteria require ("gate report shows pass/fail per
criterion with the actual value") so `GET /strategies/{id}/gate` has
something durable to read back, and so a strategy's gate history isn't
overwritten on every re-run.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

_JSONB_PORTABLE = JSONB().with_variant(JSON(), "sqlite")


class GateReportRecord(Base):
    __tablename__ = "gate_reports"

    id: Mapped[uuid.UUID] = uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False
    )
    gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # list[{name, threshold, actual, passed, detail}] -- see app.backtest.gate.GateCriterion.
    criteria: Mapped[list] = mapped_column(_JSONB_PORTABLE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
