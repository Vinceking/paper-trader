"""Phase 5 — education layer: explanations table.

BUILD_SPEC §5's schema predates Phase 5 and defines no explanations table at
all -- see app/models/explanations.py's docstring for the full deviation
note (same convention as gate_reports in 0005). This migration adds that
table: one row per closed Trade, holding the four §11.2 output sections plus
the selected §11.3 tip and provenance (source/prompt_hash).

Revision ID: 0006_phase5_explanations
Revises: 0005_phase4_backtest_gate
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_phase5_explanations"
down_revision: str | None = "0005_phase4_backtest_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "explanations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "trade_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("trades.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("entry_rationale", sa.Text, nullable=False),
        sa.Column("exit_rationale", sa.Text, nullable=False),
        sa.Column("what_went_right", sa.Text, nullable=False),
        sa.Column("what_went_wrong", sa.Text, nullable=False),
        sa.Column("tip_id", sa.String(64)),
        sa.Column("tip_body", sa.Text),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("prompt_hash", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint("uq_explanations_trade_id", "explanations", ["trade_id"])


def downgrade() -> None:
    op.drop_constraint("uq_explanations_trade_id", "explanations", type_="unique")
    op.drop_table("explanations")
