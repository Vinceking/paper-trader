"""Phase 3 — strategy engine: strategies, signals, and the deferred
signal/strategy linkage columns on orders/positions/trades.

Revision ID: 0003_phase3_strategy_engine
Revises: 0002_phase2_execution_and_risk
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003_phase3_strategy_engine"
down_revision: Union[str, None] = "0002_phase2_execution_and_risk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB_PORTABLE = JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("params", _JSONB_PORTABLE, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("gate_passed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("gate_report_id", sa.Uuid(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "slug", "name"),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("strategies.id"), nullable=False,
        ),
        sa.Column(
            "account_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("paper_accounts.id"), nullable=False,
        ),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("intent", sa.String(8), nullable=False),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("rule_text", sa.Text, nullable=False),
        sa.Column("features", _JSONB_PORTABLE, nullable=False),
        sa.Column("conditions", _JSONB_PORTABLE, nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("acted_on", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("veto_reason", sa.Text),
    )
    op.create_index(
        "ix_signals_account_ts_desc", "signals", ["account_id", sa.text("ts DESC")]
    )

    # Deferred in Phase 2 (see models/orders.py, models/positions.py history)
    # because signals/strategies didn't exist yet. Nullable and unpopulated
    # by anything in Phase 3 itself — auto-executing a strategy signal is a
    # later integration.
    op.add_column("orders", sa.Column("signal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("signals.id")))
    op.add_column(
        "positions", sa.Column("strategy_id", sa.Uuid(as_uuid=True), sa.ForeignKey("strategies.id"))
    )
    op.add_column(
        "positions", sa.Column("entry_signal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("signals.id"))
    )
    op.add_column(
        "trades", sa.Column("strategy_id", sa.Uuid(as_uuid=True), sa.ForeignKey("strategies.id"))
    )
    op.add_column(
        "trades", sa.Column("entry_signal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("signals.id"))
    )
    op.add_column(
        "trades", sa.Column("exit_signal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("signals.id"))
    )


def downgrade() -> None:
    op.drop_column("trades", "exit_signal_id")
    op.drop_column("trades", "entry_signal_id")
    op.drop_column("trades", "strategy_id")
    op.drop_column("positions", "entry_signal_id")
    op.drop_column("positions", "strategy_id")
    op.drop_column("orders", "signal_id")
    op.drop_index("ix_signals_account_ts_desc", table_name="signals")
    op.drop_table("signals")
    op.drop_table("strategies")
