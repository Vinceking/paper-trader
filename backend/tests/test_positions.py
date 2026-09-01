"""Position/trade lifecycle tests. Phase 2 acceptance criteria:

"Closing produces a trades row with correct gross/net P&L and R multiple."
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.execution.positions import OpenPosition, close_position

OPENED_AT = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
CLOSED_AT = OPENED_AT + timedelta(minutes=23)


def long_position(**overrides) -> OpenPosition:
    base = dict(
        symbol="XLF", side="buy", qty=Decimal("50"),
        avg_entry_price=Decimal("100.00"), stop_price=Decimal("98.00"),
        opened_at=OPENED_AT,
    )
    base.update(overrides)
    return OpenPosition(**base)


class TestWinningTrade:
    def test_gross_net_and_r_multiple(self):
        trade = close_position(
            long_position(),
            exit_price=Decimal("105.00"), exit_qty=Decimal("50"),
            entry_friction=Decimal("12.50"), exit_friction=Decimal("11.80"),
            closed_at=CLOSED_AT, exit_reason="target",
        )
        assert trade.gross_pnl == Decimal("250.00")        # (105-100)*50
        assert trade.total_friction == Decimal("24.30")     # 12.50 + 11.80
        assert trade.net_pnl == Decimal("225.70")           # 250.00 - 24.30
        # initial_risk = (100-98)*50 = 100.00 -> r = 225.70/100.00
        assert trade.r_multiple == Decimal("2.2570")
        assert trade.exit_reason == "target"
        assert trade.opened_at == OPENED_AT
        assert trade.closed_at == CLOSED_AT


class TestLosingTradeAtStop:
    def test_friction_makes_the_loss_worse_than_one_r(self):
        """A stop hit exactly at 1R still nets worse than -1R after friction —
        this is the exact lesson BUILD_SPEC §9.3 wants the friction counter to
        teach."""
        trade = close_position(
            long_position(),
            exit_price=Decimal("98.00"), exit_qty=Decimal("50"),
            entry_friction=Decimal("12.50"), exit_friction=Decimal("11.00"),
            closed_at=CLOSED_AT, exit_reason="stop",
        )
        assert trade.gross_pnl == Decimal("-100.00")       # (98-100)*50
        assert trade.total_friction == Decimal("23.50")
        assert trade.net_pnl == Decimal("-123.50")
        # initial_risk = 100.00 -> r = -123.50/100.00 = -1.235, worse than -1R
        assert trade.r_multiple == Decimal("-1.2350")
        assert trade.r_multiple < Decimal("-1.0")


class TestMissingOrDegenerateStop:
    def test_no_stop_price_yields_no_r_multiple(self):
        trade = close_position(
            long_position(stop_price=None),
            exit_price=Decimal("105.00"), exit_qty=Decimal("50"),
            entry_friction=Decimal("0"), exit_friction=Decimal("0"),
            closed_at=CLOSED_AT, exit_reason="manual",
        )
        assert trade.r_multiple is None
        assert trade.net_pnl == Decimal("250.00")  # P&L is still computed correctly

    def test_stop_equal_to_entry_yields_no_r_multiple(self):
        """Zero initial risk can't produce a multiple — avoid dividing by zero."""
        trade = close_position(
            long_position(stop_price=Decimal("100.00")),
            exit_price=Decimal("105.00"), exit_qty=Decimal("50"),
            entry_friction=Decimal("0"), exit_friction=Decimal("0"),
            closed_at=CLOSED_AT, exit_reason="manual",
        )
        assert trade.r_multiple is None
