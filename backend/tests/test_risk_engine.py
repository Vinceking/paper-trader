"""Risk engine tests — every veto plus sizing/clamping. BUILD_SPEC §7.4.

Phase 2 acceptance criteria: exceeding daily loss halts new entries (and, per
the caller's contract, writes a risk_events row — verified at the API layer
in test_orders_api.py). This file covers the pure decision logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.risk.engine import AccountState, RiskEngine, RiskSettingsInput, RiskSignal

NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)

DEFAULT_SETTINGS = RiskSettingsInput(
    risk_per_trade_pct=Decimal("0.01"),
    max_daily_loss_pct=Decimal("0.03"),
    max_open_positions=3,
    max_trades_per_day=10,
    max_position_pct=Decimal("0.20"),
    cooldown_after_losses=3,
    cooldown_minutes=30,
)


def healthy_account(**overrides) -> AccountState:
    base = dict(
        equity=Decimal("100000"),
        starting_equity_today=Decimal("100000"),
        realized_pnl_today=Decimal("0"),
        open_positions_count=0,
        trades_today_count=0,
        consecutive_losses=0,
        last_loss_at=None,
        now=NOW,
        minutes_until_close=60.0,
    )
    base.update(overrides)
    return AccountState(**base)


def entry_signal(**overrides) -> RiskSignal:
    base = dict(
        symbol="XLF", side="buy", intent="entry",
        entry_price=Decimal("50.00"), stop_price=Decimal("49.00"),
    )
    base.update(overrides)
    return RiskSignal(**base)


class TestHappyPath:
    def test_approves_and_sizes_and_clamps(self):
        # risk_dollars = 1000, stop_distance = 1 -> qty = 1000
        # notional = 1000*50 = 50_000 > cap (20_000) -> clamp to 400
        decision = RiskEngine().evaluate(entry_signal(), healthy_account(), DEFAULT_SETTINGS)
        assert decision.approved is True
        assert decision.qty == Decimal("400")
        assert decision.veto_reason is None


class TestDailyLossHalt:
    def test_vetoes_at_the_threshold(self):
        account = healthy_account(realized_pnl_today=Decimal("-3000"))  # exactly 3% of 100k
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.approved is False
        assert decision.veto_reason == "daily_halt"

    def test_does_not_veto_below_threshold(self):
        account = healthy_account(realized_pnl_today=Decimal("-2999"))
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason != "daily_halt"

    def test_positive_pnl_never_triggers_halt(self):
        account = healthy_account(realized_pnl_today=Decimal("5000"))
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason != "daily_halt"


class TestCooldown:
    def test_vetoes_within_cooldown_window(self):
        account = healthy_account(
            consecutive_losses=3, last_loss_at=NOW - timedelta(minutes=10),
        )
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason == "cooldown"

    def test_allows_after_cooldown_expires(self):
        account = healthy_account(
            consecutive_losses=3, last_loss_at=NOW - timedelta(minutes=31),
        )
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason != "cooldown"

    def test_below_loss_threshold_is_not_in_cooldown(self):
        account = healthy_account(
            consecutive_losses=2, last_loss_at=NOW - timedelta(minutes=1),
        )
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason != "cooldown"


class TestMaxOpenPositions:
    def test_vetoes_at_cap(self):
        account = healthy_account(open_positions_count=3)
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason == "max_positions"

    def test_allows_below_cap(self):
        account = healthy_account(open_positions_count=2)
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason != "max_positions"


class TestMaxTradesPerDay:
    def test_vetoes_at_cap(self):
        account = healthy_account(trades_today_count=10)
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason == "max_trades"


class TestNearClose:
    def test_vetoes_entries_within_ten_minutes_of_close(self):
        account = healthy_account(minutes_until_close=5.0)
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason == "near_close"

    def test_allows_entries_further_out(self):
        account = healthy_account(minutes_until_close=15.0)
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason != "near_close"

    def test_unknown_close_time_does_not_veto(self):
        account = healthy_account(minutes_until_close=None)
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason != "near_close"


class TestMissingStop:
    def test_entry_without_stop_is_rejected(self):
        """CLAUDE.md rule 6: every entry must define its stop."""
        decision = RiskEngine().evaluate(
            entry_signal(stop_price=None), healthy_account(), DEFAULT_SETTINGS,
        )
        assert decision.approved is False
        assert decision.veto_reason == "missing_stop"


class TestSizeZero:
    def test_zero_stop_distance_vetoes(self):
        decision = RiskEngine().evaluate(
            entry_signal(entry_price=Decimal("50"), stop_price=Decimal("50")),
            healthy_account(), DEFAULT_SETTINGS,
        )
        assert decision.veto_reason == "size_zero"

    def test_clamped_to_zero_still_vetoes(self):
        tiny_cap_settings = RiskSettingsInput(
            risk_per_trade_pct=Decimal("0.01"), max_daily_loss_pct=Decimal("0.03"),
            max_open_positions=3, max_trades_per_day=10,
            max_position_pct=Decimal("0.0001"), cooldown_after_losses=3, cooldown_minutes=30,
        )
        decision = RiskEngine().evaluate(entry_signal(), healthy_account(), tiny_cap_settings)
        assert decision.veto_reason == "size_zero"


class TestExitsAreNeverVetoed:
    def test_exit_bypasses_every_veto(self):
        # Deliberately construct an account that would fail every entry veto.
        hostile_account = healthy_account(
            realized_pnl_today=Decimal("-10000"),
            open_positions_count=99,
            trades_today_count=99,
            consecutive_losses=99,
            last_loss_at=NOW,
            minutes_until_close=1.0,
        )
        decision = RiskEngine().evaluate(
            entry_signal(intent="exit", stop_price=None), hostile_account, DEFAULT_SETTINGS,
        )
        assert decision.approved is True
        assert decision.veto_reason is None


class TestVetoPriorityOrder:
    def test_daily_halt_takes_priority_over_max_positions(self):
        account = healthy_account(realized_pnl_today=Decimal("-5000"), open_positions_count=5)
        decision = RiskEngine().evaluate(entry_signal(), account, DEFAULT_SETTINGS)
        assert decision.veto_reason == "daily_halt"
