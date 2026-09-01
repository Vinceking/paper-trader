"""Friction model tests — every component against hand-computed values.

BUILD_SPEC §9, Phase 2 acceptance criteria: "Unit tests cover every friction
component against hand-computed values." Each helper is tested in isolation
first, then a couple of full apply_friction() scenarios tie them together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.execution.friction import (
    FrictionConfig,
    FrictionInput,
    apply_friction,
    commission_cost,
    finra_taf,
    half_spread_per_share,
    mid_price,
    sec_fee,
    slippage_per_share,
)
from app.market_calendar import in_open_or_close_window

CFG = FrictionConfig()  # defaults from BUILD_SPEC §9.2

# A Monday in EDT (UTC-4) so ET offsets are simple to hand-verify.
MID_SESSION = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)  # 13:00 ET
NEAR_OPEN = datetime(2026, 8, 31, 13, 31, tzinfo=UTC)  # 09:31 ET


class TestMidPrice:
    def test_mid_is_average_of_bid_ask(self):
        assert mid_price(Decimal("99.90"), Decimal("100.10")) == Decimal("100.00")


class TestHalfSpread:
    def test_quoted_spread_wider_than_floor(self):
        # spread = 0.20, floor = 100*2bps = 0.02 -> spread wins -> half*1.5
        result = half_spread_per_share(Decimal("99.90"), Decimal("100.10"), CFG)
        assert result == Decimal("0.15")

    def test_floors_at_min_spread_bps_when_quote_is_tighter(self):
        # spread = 0.01, floor = 100*2bps = 0.02 -> floor wins -> half*1.5
        result = half_spread_per_share(Decimal("99.995"), Decimal("100.005"), CFG)
        assert result == Decimal("0.015")


class TestSlippage:
    def test_scales_with_order_size_below_typical_volume(self):
        # size_factor = 100/10_000 = 0.01 -> slip = 1.00 * 0.05 * 0.51
        result = slippage_per_share(Decimal("1.00"), Decimal("100"), Decimal("10000"), CFG)
        assert result == Decimal("0.0255")

    def test_size_factor_caps_at_one(self):
        # qty >= typical volume -> size_factor clamped to 1 -> slip = atr*0.05*1.5
        result = slippage_per_share(Decimal("1.00"), Decimal("20000"), Decimal("10000"), CFG)
        assert result == Decimal("0.075")

    def test_zero_typical_volume_treated_as_worst_case(self):
        """No liquidity data -> assume full market impact, not division by zero."""
        result = slippage_per_share(Decimal("1.00"), Decimal("1"), Decimal("0"), CFG)
        assert result == Decimal("0.075")


class TestCommission:
    def test_zero_by_default(self):
        assert commission_cost(Decimal("100"), CFG) == Decimal("0")

    def test_per_share_floors_at_minimum(self):
        cfg = FrictionConfig(commission_per_share=Decimal("0.005"), commission_min=Decimal("1.00"))
        assert commission_cost(Decimal("100"), cfg) == Decimal("1.00")  # 0.50 < min
        assert commission_cost(Decimal("500"), cfg) == Decimal("2.50")  # 2.50 > min


class TestRegulatoryFees:
    def test_sec_fee_matches_finra_rate_notice(self):
        # $20.60 per $1,000,000 of principal, effective April 4 2026.
        assert sec_fee(Decimal("1000000"), CFG) == Decimal("20.60")

    def test_finra_taf_per_share(self):
        assert finra_taf(Decimal("1000"), CFG) == Decimal("0.166")

    def test_finra_taf_caps_at_8_30(self):
        assert finra_taf(Decimal("100000"), CFG) == Decimal("8.30")


class TestOpenCloseWindow:
    def test_within_five_minutes_of_open(self):
        assert in_open_or_close_window(NEAR_OPEN) is True

    def test_outside_open_window(self):
        just_past = datetime(2026, 8, 31, 13, 36, tzinfo=UTC)  # 09:36 ET
        assert in_open_or_close_window(just_past) is False

    def test_within_five_minutes_of_close(self):
        near_close = datetime(2026, 8, 31, 19, 56, tzinfo=UTC)  # 15:56 ET
        assert in_open_or_close_window(near_close) is True

    def test_after_close_is_not_flagged(self):
        after_close = datetime(2026, 8, 31, 20, 1, tzinfo=UTC)  # 16:01 ET
        assert in_open_or_close_window(after_close) is False

    def test_mid_session_is_not_flagged(self):
        assert in_open_or_close_window(MID_SESSION) is False


class TestApplyFrictionBuy:
    def test_buy_fill_price_and_breakdown(self):
        inp = FrictionInput(
            side="buy", qty=Decimal("100"), ts=MID_SESSION,
            bid=Decimal("99.90"), ask=Decimal("100.10"),
            atr=Decimal("1.00"), typical_bar_volume=Decimal("10000"),
        )
        result = apply_friction(inp, CFG)

        assert result.reference_price == Decimal("100.00")
        assert result.spread_cost == Decimal("15.00")       # 0.15 * 100
        assert result.slippage_cost == Decimal("2.55")       # 0.0255 * 100
        assert result.commission == Decimal("0")
        assert result.reg_fees == Decimal("0")               # buys never pay SEC/TAF
        assert result.fill_price == Decimal("100.1755")      # mid + half_spread + slip
        assert result.total_friction == Decimal("17.55")

    def test_buy_near_open_doubles_spread_and_slippage_only(self):
        inp = FrictionInput(
            side="buy", qty=Decimal("100"), ts=NEAR_OPEN,
            bid=Decimal("99.90"), ask=Decimal("100.10"),
            atr=Decimal("1.00"), typical_bar_volume=Decimal("10000"),
        )
        result = apply_friction(inp, CFG)

        assert result.spread_cost == Decimal("30.00")        # doubled
        assert result.slippage_cost == Decimal("5.10")       # doubled
        assert result.reg_fees == Decimal("0")                # unaffected by the penalty
        assert result.fill_price == Decimal("100.351")
        assert result.total_friction == Decimal("35.10")


class TestApplyFrictionSell:
    def test_sell_fill_price_includes_sec_and_taf_fees(self):
        inp = FrictionInput(
            side="sell", qty=Decimal("200"), ts=MID_SESSION,
            bid=Decimal("49.98"), ask=Decimal("50.02"),
            atr=Decimal("0.50"), typical_bar_volume=Decimal("2000"),
        )
        result = apply_friction(inp, CFG)

        assert result.reference_price == Decimal("50.00")
        assert result.spread_cost == Decimal("6.00")          # 0.03 * 200
        assert result.slippage_cost == Decimal("3.00")        # 0.015 * 200
        assert result.fill_price == Decimal("49.955")         # mid - (half_spread + slip)
        # principal = 49.955 * 200 = 9991.00
        # sec_fee = 9991.00 * 0.0000206 = 0.2058146
        # taf = 200 * 0.000166 = 0.0332 (well under the 8.30 cap)
        assert result.reg_fees == Decimal("0.2390146")
        assert result.total_friction == Decimal("9.2390146")
