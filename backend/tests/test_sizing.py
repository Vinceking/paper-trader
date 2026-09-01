"""Fixed-fractional sizing tests, hand-computed. BUILD_SPEC §7.4."""

from __future__ import annotations

from decimal import Decimal

from app.risk.sizing import clamp_to_max_position, fixed_fractional_qty


class TestFixedFractionalQty:
    def test_basic_case(self):
        # risk_dollars = 100_000 * 0.01 = 1000; stop_distance = 2 -> qty = 500
        qty = fixed_fractional_qty(
            equity=Decimal("100000"), risk_per_trade_pct=Decimal("0.01"),
            entry_price=Decimal("100"), stop_price=Decimal("98"),
        )
        assert qty == Decimal("500")

    def test_floors_fractional_shares(self):
        # risk_dollars = 1000; stop_distance = 3 -> 333.33... -> floors to 333
        qty = fixed_fractional_qty(
            equity=Decimal("100000"), risk_per_trade_pct=Decimal("0.01"),
            entry_price=Decimal("100"), stop_price=Decimal("97"),
        )
        assert qty == Decimal("333")

    def test_zero_stop_distance_is_zero_shares(self):
        """A stop equal to entry can't be sized — treated as zero, not a crash."""
        qty = fixed_fractional_qty(
            equity=Decimal("100000"), risk_per_trade_pct=Decimal("0.01"),
            entry_price=Decimal("100"), stop_price=Decimal("100"),
        )
        assert qty == Decimal("0")

    def test_stop_above_or_below_entry_uses_absolute_distance(self):
        long_qty = fixed_fractional_qty(
            Decimal("100000"), Decimal("0.01"), Decimal("100"), Decimal("98"),
        )
        short_qty = fixed_fractional_qty(
            Decimal("100000"), Decimal("0.01"), Decimal("100"), Decimal("102"),
        )
        assert long_qty == short_qty == Decimal("500")


class TestClampToMaxPosition:
    def test_clamps_down_when_notional_exceeds_cap(self):
        # notional = 500*100 = 50_000; cap = 100_000*0.20 = 20_000 -> clamp to 200
        qty = clamp_to_max_position(
            qty=Decimal("500"), entry_price=Decimal("100"),
            equity=Decimal("100000"), max_position_pct=Decimal("0.20"),
        )
        assert qty == Decimal("200")

    def test_leaves_qty_unchanged_when_within_cap(self):
        qty = clamp_to_max_position(
            qty=Decimal("100"), entry_price=Decimal("100"),
            equity=Decimal("100000"), max_position_pct=Decimal("0.20"),
        )
        assert qty == Decimal("100")

    def test_can_clamp_all_the_way_to_zero(self):
        # cap = 100_000*0.0001 = 10; entry=50 -> floor(10/50) = 0
        qty = clamp_to_max_position(
            qty=Decimal("500"), entry_price=Decimal("50"),
            equity=Decimal("100000"), max_position_pct=Decimal("0.0001"),
        )
        assert qty == Decimal("0")
