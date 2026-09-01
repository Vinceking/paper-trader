"""Phase 2 — execution + risk: orders, fills, positions, trades, risk_settings,
risk_events.

`orders.signal_id` and the strategy/signal linkage on positions/trades are
deferred to Phase 3, once `strategies`/`signals` exist (see models/orders.py).

Revision ID: 0002_phase2_execution_and_risk
Revises: 0001_phase1_data_spine
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_phase2_execution_and_risk"
down_revision: Union[str, None] = "0001_phase1_data_spine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB_PORTABLE = JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("paper_accounts.id"), nullable=False,
        ),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("qty", sa.Numeric(18, 6), nullable=False),
        sa.Column("order_type", sa.String(8), nullable=False, server_default="market"),
        sa.Column("limit_price", sa.Numeric(18, 4)),
        sa.Column("stop_price", sa.Numeric(18, 4)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("broker_order_id", sa.String(64)),
    )

    op.create_table(
        "fills",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id", sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False
        ),
        sa.Column("qty", sa.Numeric(18, 6), nullable=False),
        sa.Column("reference_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("fill_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("slippage_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("spread_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("commission", sa.Numeric(18, 4), nullable=False),
        sa.Column("reg_fees", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "filled_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("paper_accounts.id"), nullable=False,
        ),
        # Not in BUILD_SPEC §5 — see models/positions.py docstring: needed to
        # trace a position back to its entry Fill's friction on close.
        sa.Column(
            "entry_order_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("orders.id"), nullable=False,
        ),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("qty", sa.Numeric(18, 6), nullable=False),
        sa.Column("avg_entry_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("stop_price", sa.Numeric(18, 4)),
        sa.Column("target_price", sa.Numeric(18, 4)),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("paper_accounts.id"), nullable=False,
        ),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("qty", sa.Numeric(18, 6), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("exit_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gross_pnl", sa.Numeric(18, 4), nullable=False),
        sa.Column("total_friction", sa.Numeric(18, 4), nullable=False),
        sa.Column("net_pnl", sa.Numeric(18, 4), nullable=False),
        sa.Column("r_multiple", sa.Numeric(8, 4)),
        sa.Column("exit_reason", sa.String(16), nullable=False),
        sa.Column("benchmark_return_pct", sa.Numeric(10, 6)),
    )
    op.create_index(
        "ix_trades_account_closed_desc", "trades", ["account_id", sa.text("closed_at DESC")]
    )

    op.create_table(
        "risk_settings",
        sa.Column(
            "account_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("paper_accounts.id"), primary_key=True,
        ),
        sa.Column("risk_per_trade_pct", sa.Numeric(6, 4), nullable=False, server_default="0.01"),
        sa.Column("max_daily_loss_pct", sa.Numeric(6, 4), nullable=False, server_default="0.03"),
        sa.Column("max_open_positions", sa.Integer, nullable=False, server_default="3"),
        sa.Column("max_trades_per_day", sa.Integer, nullable=False, server_default="10"),
        sa.Column("max_position_pct", sa.Numeric(6, 4), nullable=False, server_default="0.20"),
        sa.Column("cooldown_after_losses", sa.Integer, nullable=False, server_default="3"),
        sa.Column("cooldown_minutes", sa.Integer, nullable=False, server_default="30"),
    )

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("paper_accounts.id"), nullable=False,
        ),
        sa.Column(
            "ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("detail", _JSONB_PORTABLE, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("risk_events")
    op.drop_table("risk_settings")
    op.drop_index("ix_trades_account_closed_desc", table_name="trades")
    op.drop_table("trades")
    op.drop_table("positions")
    op.drop_table("fills")
    op.drop_table("orders")
