"""The backtest gate's criteria math. BUILD_SPEC §8.5, Phase 4 acceptance
criteria: "gate report shows pass/fail per criterion with the actual value."

Every test builds a hand-computable list of `ClosedTrade`s (from
`app.execution.positions`, the exact dataclass the live system and
`app.backtest.runner` both produce) and asserts `compute_gate_report`'s
numbers against arithmetic worked out in each test's comment -- not against
another implementation of the same formula.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.gate import compute_gate_report
from app.execution.positions import ClosedTrade

_OOS_START = datetime(2024, 1, 1, tzinfo=UTC)
_OOS_END_13MO = datetime(2025, 2, 1, tzinfo=UTC)  # 13 months
_OOS_END_3MO = datetime(2024, 4, 1, tzinfo=UTC)  # 3 months
_STARTING_EQUITY = Decimal("100000")


def _trade(net_pnl: Decimal, idx: int) -> ClosedTrade:
    ts = _OOS_START + timedelta(days=idx)
    return ClosedTrade(
        symbol="SPY", side="buy", qty=Decimal("10"),
        entry_price=Decimal("100"), exit_price=Decimal("100") + net_pnl / Decimal(10),
        opened_at=ts - timedelta(hours=1), closed_at=ts,
        gross_pnl=net_pnl, total_friction=Decimal("0"), net_pnl=net_pnl,
        r_multiple=None, exit_reason="signal",
    )


def _by_name(report, name: str):
    return next(c for c in report.criteria if c.name == name)


class TestAllCriteriaPass:
    def test_gate_passes_when_every_criterion_holds(self):
        # 70 wins of +100 (=7000), 30 losses of -100 (=3000), wins first so
        # the running equity curve only ever rises then gives a small,
        # shallow pullback -- max drawdown = 3000 / 107000 = 2.80%.
        trades = [_trade(Decimal("100"), i) for i in range(70)]
        trades += [_trade(Decimal("-100"), 70 + i) for i in range(30)]

        report = compute_gate_report(
            trades=trades,
            oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.05"),
            out_of_sample_return_pct=Decimal("0.04"),  # 4000 net / 100000
            spy_buy_hold_return_pct=Decimal("0.02"),
            starting_equity=_STARTING_EQUITY,
        )

        assert report.gate_passed is True
        assert all(c.passed for c in report.criteria)

        sample = _by_name(report, "sample_size")
        assert sample.actual == 100.0

        oos = _by_name(report, "oos_period_months")
        assert oos.actual == pytest.approx(13.0, abs=0.2)

        expectancy = _by_name(report, "expectancy_after_friction")
        assert expectancy.actual == pytest.approx(40.0, abs=0.01)  # (7000-3000)/100

        pf = _by_name(report, "profit_factor")
        assert pf.actual == pytest.approx(7000 / 3000, abs=0.001)

        dd = _by_name(report, "max_drawdown_pct")
        assert dd.actual == pytest.approx(3000 / 107000, abs=0.001)

        wfe = _by_name(report, "walk_forward_efficiency")
        assert wfe.actual == pytest.approx(0.04 / 0.05, abs=0.001)

        spy = _by_name(report, "beats_spy_buy_and_hold")
        assert spy.actual == pytest.approx(0.04, abs=0.001)


class TestFailsSampleSizeOnly:
    def test_too_few_trades(self):
        trades = [_trade(Decimal("100"), i) for i in range(50)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.05"), out_of_sample_return_pct=Decimal("0.05"),
            spy_buy_hold_return_pct=Decimal("0.01"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        sample = _by_name(report, "sample_size")
        assert sample.passed is False
        assert sample.actual == 50.0
        assert sample.threshold == 100.0


class TestFailsOosPeriodOnly:
    def test_window_too_short(self):
        trades = [_trade(Decimal("100"), i % 90) for i in range(120)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_3MO,
            in_sample_return_pct=Decimal("0.05"), out_of_sample_return_pct=Decimal("0.05"),
            spy_buy_hold_return_pct=Decimal("0.01"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        oos = _by_name(report, "oos_period_months")
        assert oos.passed is False
        assert oos.actual < 12.0


class TestFailsExpectancyOnly:
    def test_negative_mean_net_pnl(self):
        # Mean net_pnl <= 0 mathematically forces profit_factor < 1.2 too
        # (see module note in app/backtest/gate.py's docstring reasoning) --
        # both fail together here, which is the honest, unavoidable result
        # of a losing trade population, not a test bug.
        trades = [_trade(Decimal("50"), i) for i in range(40)]
        trades += [_trade(Decimal("-100"), 40 + i) for i in range(60)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.05"), out_of_sample_return_pct=Decimal("-0.04"),
            spy_buy_hold_return_pct=Decimal("0.01"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        expectancy = _by_name(report, "expectancy_after_friction")
        assert expectancy.passed is False
        assert expectancy.actual < 0


class TestFailsProfitFactorButExpectancyStillPositive:
    def test_profit_factor_below_threshold_with_positive_expectancy(self):
        # 60 wins of +22 (=1320), 40 losses of -30 (=1200):
        # profit_factor = 1320/1200 = 1.10 (< 1.2, fails)
        # mean = (1320-1200)/100 = 1.2 (> 0, expectancy still passes)
        trades = [_trade(Decimal("22"), i) for i in range(60)]
        trades += [_trade(Decimal("-30"), 60 + i) for i in range(40)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.05"), out_of_sample_return_pct=Decimal("0.001"),
            spy_buy_hold_return_pct=Decimal("-0.01"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        pf = _by_name(report, "profit_factor")
        assert pf.passed is False
        assert pf.actual == pytest.approx(1320 / 1200, abs=0.001)
        expectancy = _by_name(report, "expectancy_after_friction")
        assert expectancy.passed is True


class TestProfitFactorSentinelWhenNoLosses:
    def test_no_losing_trades_uses_sentinel_and_passes(self):
        trades = [_trade(Decimal("10"), i) for i in range(100)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.05"), out_of_sample_return_pct=Decimal("0.01"),
            spy_buy_hold_return_pct=Decimal("0.0"), starting_equity=_STARTING_EQUITY,
        )
        pf = _by_name(report, "profit_factor")
        assert pf.passed is True
        assert pf.actual == 999.0


class TestFailsMaxDrawdownOnly:
    def test_large_early_losing_streak_then_recovery(self):
        # 30 losses of -1000 FIRST (peak 100000 -> trough 70000, dd = 30%,
        # fails), then 70 wins of +1000 (recovers to 140000). Overall stats
        # (pf, expectancy, sample size, oos period) all still pass -- this
        # isolates max_drawdown as the one failing criterion.
        trades = [_trade(Decimal("-1000"), i) for i in range(30)]
        trades += [_trade(Decimal("1000"), 30 + i) for i in range(70)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.10"), out_of_sample_return_pct=Decimal("0.40"),
            spy_buy_hold_return_pct=Decimal("0.05"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        dd = _by_name(report, "max_drawdown_pct")
        assert dd.passed is False
        assert dd.actual == pytest.approx(0.30, abs=0.001)

        # Confirm the other criteria genuinely do pass in this fixture.
        assert _by_name(report, "sample_size").passed is True
        assert _by_name(report, "expectancy_after_friction").passed is True
        assert _by_name(report, "profit_factor").passed is True
        assert _by_name(report, "oos_period_months").passed is True


class TestFailsWalkForwardEfficiencyOnly:
    def test_oos_return_far_below_in_sample_return(self):
        trades = [_trade(Decimal("100"), i) for i in range(70)]
        trades += [_trade(Decimal("-100"), 70 + i) for i in range(30)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.50"), out_of_sample_return_pct=Decimal("0.04"),
            spy_buy_hold_return_pct=Decimal("0.02"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        wfe = _by_name(report, "walk_forward_efficiency")
        assert wfe.passed is False
        assert wfe.actual == pytest.approx(0.04 / 0.50, abs=0.001)

    def test_non_positive_in_sample_return_fails_cleanly(self):
        trades = [_trade(Decimal("100"), i) for i in range(70)]
        trades += [_trade(Decimal("-100"), 70 + i) for i in range(30)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0"), out_of_sample_return_pct=Decimal("0.04"),
            spy_buy_hold_return_pct=Decimal("0.02"), starting_equity=_STARTING_EQUITY,
        )
        wfe = _by_name(report, "walk_forward_efficiency")
        assert wfe.passed is False
        assert wfe.actual == 0.0


class TestFailsBeatsSpyOnly:
    def test_strategy_underperforms_spy(self):
        trades = [_trade(Decimal("100"), i) for i in range(70)]
        trades += [_trade(Decimal("-100"), 70 + i) for i in range(30)]
        report = compute_gate_report(
            trades=trades, oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0.05"), out_of_sample_return_pct=Decimal("0.04"),
            spy_buy_hold_return_pct=Decimal("0.20"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        spy = _by_name(report, "beats_spy_buy_and_hold")
        assert spy.passed is False
        assert spy.threshold == 0.20


class TestEmptyTradeList:
    def test_no_trades_fails_every_data_dependent_criterion(self):
        report = compute_gate_report(
            trades=[], oos_start=_OOS_START, oos_end=_OOS_END_13MO,
            in_sample_return_pct=Decimal("0"), out_of_sample_return_pct=Decimal("0"),
            spy_buy_hold_return_pct=Decimal("0"), starting_equity=_STARTING_EQUITY,
        )
        assert report.gate_passed is False
        assert _by_name(report, "sample_size").actual == 0.0
        assert _by_name(report, "expectancy_after_friction").passed is False
        # No trades -> no drawdown to have occurred -- this one legitimately
        # passes even though the overall gate still correctly fails.
        assert _by_name(report, "max_drawdown_pct").passed is True
