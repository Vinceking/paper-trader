"""Phase 4 — backtest gate: gate_reports table.

`strategies.gate_report_id` (added in 0003) had no defined target table in
BUILD_SPEC §5's literal SQL -- see app/models/gate_reports.py's docstring
for the deviation note. This migration adds that table.

Revision ID: 0005_phase4_backtest_gate
Revises: 0003_phase3_strategy_engine
Create Date: 2026-09-05

NOTE: at the time this migration was authored, 0003_phase3_strategy_engine
was the latest revision under alembic/versions/ -- if a 0004_* migration
exists by the time this merges, rebase down_revision onto it instead of
0003 (see the Phase 4 task brief).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0005_phase4_backtest_gate"
down_revision: str | None = "0003_phase3_strategy_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB_PORTABLE = JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "gate_reports",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("gate_passed", sa.Boolean, nullable=False),
        sa.Column("criteria", _JSONB_PORTABLE, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_gate_reports_strategy_id", "gate_reports", ["strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_gate_reports_strategy_id", table_name="gate_reports")
    op.drop_table("gate_reports")
