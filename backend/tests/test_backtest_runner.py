"""The event-driven backtest runner. BUILD_SPEC §8.5, Phase 4 acceptance
criteria: "Backtest and live paper share the same friction code path (assert
in tests)."

Uses a small scripted `Strategy` (fires a deterministic entry on one bar and
a deterministic exit on another) over synthetic bars, rather than one of the
four real strategies -- this isolates the runner's own mechanics (fill
timing, friction wiring, no-lookahead) from any particular strategy's
condition logic, and keeps every number in this file independently
recomputable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.backtest.runner import BacktestConfig, _synthetic_quote, run_backtest
from app.execution.friction import FrictionInput, apply_friction
from app.execution.positions import OpenPosition
from app.ingest.bars import FinalBar
from app.strategies.base import BarContext, Condition, Signal, Strategy
from app.strategies.indicators import compute_indicators

_START = datetime(2024, 1, 1, tzinfo=UTC)
_ENTRY_DECISION_IDX = 14  # bar index the entry Signal fires on
_EXIT_DECISION_IDX = 17  # bar index the exit Signal fires on
_N_BARS = 20


def _make_bars() -> list[FinalBar]:
    bars = []
    for i in range(_N_BARS):
        close = Decimal(100) + Decimal(i)
        bars.append(
            FinalBar(
                symbol="TEST", timeframe="1Min", ts=_START + timedelta(minutes=i),
                open=close - Decimal("0.5"), high=close + Decimal("1"),
                low=close - Decimal("1"), close=close,
                volume=1000 + 10 * i, vwap=None, trade_count=5,
            )
        )
    return bars


class _ScriptedStrategy(Strategy):
    """Fires an entry on bar index `entry_idx` and an exit on `exit_idx`,
    identified by timestamp so it works regardless of how much history
    `SymbolEngine` hands back."""

    slug = "scripted_test"
    timeframe = "1Min"
    default_params: dict = {}

    def __init__(self, entry_ts, exit_ts, stop_distance: Decimal):
        super().__init__()
        self.entry_ts = entry_ts
        self.exit_ts = exit_ts
        self.stop_distance = stop_distance

    def evaluate(self, ctx: BarContext) -> Signal | None:
        if ctx.bar.ts != self.entry_ts:
            return None
        return Signal(
            side="buy", intent="entry", symbol=ctx.symbol,
            rule_id="scripted.entry", rule_text="scripted test entry",
            features={}, conditions=[
                Condition(name="scripted", description="scripted", operator="==",
                          threshold=1.0, actual=1.0, passed=True)
            ],
            stop_price=ctx.bar.close - self.stop_distance,
        )

    def manage(self, ctx: BarContext, position: OpenPosition) -> Signal | None:
        if ctx.bar.ts != self.exit_ts:
            return None
        return Signal(
            side="sell", intent="exit", symbol=ctx.symbol,
            rule_id="scripted.exit", rule_text="scripted test exit",
            features={}, conditions=[
                Condition(name="scripted", description="scripted", operator="==",
                          threshold=1.0, actual=1.0, passed=True)
            ],
            stop_price=None,
        )


class TestSignalsExecuteAtNextBarOpen:
    def test_entry_and_exit_fill_one_bar_after_the_signal(self):
        bars = _make_bars()
        strategy = _ScriptedStrategy(
            entry_ts=bars[_ENTRY_DECISION_IDX].ts, exit_ts=bars[_EXIT_DECISION_IDX].ts,
            stop_distance=Decimal("5"),
        )
        result = run_backtest(strategy, "TEST", bars)

        assert len(result.trades) == 1
        trade = result.trades[0]
        # Entry/exit opened_at/closed_at must be the FILL bar's ts (decision
        # bar + 1), never the decision bar's own ts -- BUILD_SPEC §8.5 rule 1.
        assert trade.opened_at == bars[_ENTRY_DECISION_IDX + 1].ts
        assert trade.closed_at == bars[_EXIT_DECISION_IDX + 1].ts


class TestFrictionCodePathIsTheLiteralFunction:
    def test_trade_friction_matches_independent_apply_friction_calls(self):
        """The actual Phase 4 acceptance criterion: reconstruct, bar for
        bar, exactly the FrictionInput the runner must have used for each
        leg (per its own documented rules: synthetic bid/ask centered on the
        fill bar's open, decision-bar ATR, pre-fill-bar rolling volume
        average) and assert the trade's total_friction is byte-for-byte the
        sum of two independent `apply_friction` calls -- proving the runner
        calls the literal function with the inputs it claims to, not a
        parallel reimplementation of the friction math.
        """
        bars = _make_bars()
        entry_fill_idx = _ENTRY_DECISION_IDX + 1
        exit_fill_idx = _EXIT_DECISION_IDX + 1

        strategy = _ScriptedStrategy(
            entry_ts=bars[_ENTRY_DECISION_IDX].ts, exit_ts=bars[_EXIT_DECISION_IDX].ts,
            stop_distance=Decimal("5"),
        )
        config = BacktestConfig()
        result = run_backtest(strategy, "TEST", bars, config=config)
        assert len(result.trades) == 1
        trade = result.trades[0]
        qty = trade.qty  # reuse the runner's own sizing; not what this test checks

        # ---- entry leg ----
        entry_atr = compute_indicators(bars[: _ENTRY_DECISION_IDX + 1]).atr_14
        entry_bid, entry_ask = _synthetic_quote(bars[entry_fill_idx])
        entry_typical_volume = Decimal(
            sum(b.volume for b in bars[:entry_fill_idx])
        ) / Decimal(entry_fill_idx)
        expected_entry_fill = apply_friction(
            FrictionInput(
                side="buy", qty=qty, ts=bars[entry_fill_idx].ts,
                bid=entry_bid, ask=entry_ask,
                atr=Decimal(str(entry_atr)), typical_bar_volume=entry_typical_volume,
            ),
            config.friction_config,
        )

        # ---- exit leg ----
        exit_atr = compute_indicators(bars[: _EXIT_DECISION_IDX + 1]).atr_14
        exit_bid, exit_ask = _synthetic_quote(bars[exit_fill_idx])
        exit_typical_volume = Decimal(
            sum(b.volume for b in bars[:exit_fill_idx])
        ) / Decimal(exit_fill_idx)
        expected_exit_fill = apply_friction(
            FrictionInput(
                side="sell", qty=qty, ts=bars[exit_fill_idx].ts,
                bid=exit_bid, ask=exit_ask,
                atr=Decimal(str(exit_atr)), typical_bar_volume=exit_typical_volume,
            ),
            config.friction_config,
        )

        expected_total_friction = (
            expected_entry_fill.total_friction + expected_exit_fill.total_friction
        )
        assert trade.total_friction == expected_total_friction
        assert trade.entry_price == expected_entry_fill.fill_price
        assert trade.exit_price == expected_exit_fill.fill_price

        # Sanity: friction is actually nonzero (both legs paid spread/slippage).
        assert trade.total_friction > 0


class TestNoLookahead:
    def test_no_trade_fires_before_the_entry_decision_bar(self):
        """A regression guard: if the runner ever executed a signal at its
        OWN bar's open (instead of the next bar's), the trade's opened_at
        would equal the decision bar's ts, one bar too early -- exactly the
        lookahead bug BUILD_SPEC §8.5 rule 1 exists to prevent."""
        bars = _make_bars()
        strategy = _ScriptedStrategy(
            entry_ts=bars[_ENTRY_DECISION_IDX].ts, exit_ts=bars[_EXIT_DECISION_IDX].ts,
            stop_distance=Decimal("5"),
        )
        result = run_backtest(strategy, "TEST", bars)
        assert result.trades[0].opened_at != bars[_ENTRY_DECISION_IDX].ts

    def test_signal_on_the_last_bar_is_never_fabricated_into_a_trade(self):
        """A signal generated on the LAST bar has no next bar to fill at --
        it must be dropped, not force-filled with a made-up price."""
        bars = _make_bars()
        strategy = _ScriptedStrategy(
            entry_ts=bars[-1].ts, exit_ts=bars[-1].ts,  # fires only on the final bar
            stop_distance=Decimal("5"),
        )
        result = run_backtest(strategy, "TEST", bars)
        assert result.trades == []
        assert result.ended_with_open_position is False


class TestOpenPositionAtEndIsNotFabricated:
    def test_unrealized_position_is_reported_open_not_force_closed(self):
        bars = _make_bars()
        # Exit never fires (exit_ts set to a timestamp that doesn't exist).
        strategy = _ScriptedStrategy(
            entry_ts=bars[_ENTRY_DECISION_IDX].ts,
            exit_ts=_START - timedelta(days=1),
            stop_distance=Decimal("5"),
        )
        result = run_backtest(strategy, "TEST", bars)
        assert result.trades == []
        assert result.ended_with_open_position is True
