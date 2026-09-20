"""Explainer tests. BUILD_SPEC §11.1/§11.2, Phase 5 acceptance criteria.

Covers, per the Phase 5 task brief:
(a) with no API key / no LLM provider, the deterministic template renders
    and the explanation persists -- the literal Phase 5 acceptance
    criterion (BUILD_SPEC §16).
(b) a manual trade with no linked signal gets an honest explanation, never
    a fabricated one.
(c) the explainer never receives raw price history and never invents a
    claim not backed by a supplied value -- asserted the same way
    ADDENDUM_LIVE_APPROVAL §7 describes ("no number that wasn't in the
    input payload").
(d) generation is idempotent -- one ExplanationRecord per trade, ever.

Runs against the shared in-memory-SQLite `db_session` fixture from
tests/conftest.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.education.explainer import (
    assemble_payload,
    build_trade_context,
    generate_explanation_for_trade,
)
from app.education.llm import Explanation, LLMExplanationError
from app.models.account import PaperAccount
from app.models.explanations import ExplanationRecord
from app.models.positions import Trade
from app.models.signals import SignalRecord
from app.models.strategies import StrategyRecord

NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)


async def _seed_account(session) -> PaperAccount:
    account = PaperAccount(
        id=uuid4(), user_id=uuid4(), name="test",
        starting_cash=Decimal("100000"), cash=Decimal("100000"), equity=Decimal("100000"),
    )
    session.add(account)
    await session.commit()
    return account


def _manual_trade(
    account_id, *, net_pnl="50.00", exit_reason="manual", closed_at=NOW, opened_at=None
) -> Trade:
    return Trade(
        account_id=account_id, symbol="XLF", side="buy", qty=Decimal("10"),
        entry_price=Decimal("40.00"), exit_price=Decimal("45.00"),
        opened_at=opened_at or (closed_at - timedelta(minutes=20)), closed_at=closed_at,
        gross_pnl=Decimal("50.00"), total_friction=Decimal("2.50"),
        net_pnl=Decimal(net_pnl), r_multiple=Decimal("1.0"), exit_reason=exit_reason,
    )


class FakeFailingProvider:
    async def explain(self, payload: dict) -> Explanation:
        raise LLMExplanationError("simulated failure")


class FakeWorkingProvider:
    async def explain(self, payload: dict) -> Explanation:
        return Explanation(
            entry_rationale="llm entry", exit_rationale="llm exit",
            what_went_right="llm right", what_went_wrong="llm wrong",
        )


class TestTemplateFallback:
    @pytest.mark.asyncio
    async def test_no_provider_renders_template_and_persists(self, db_session):
        account = await _seed_account(db_session)
        trade = _manual_trade(account.id)
        db_session.add(trade)
        await db_session.commit()

        record = await generate_explanation_for_trade(db_session, trade, llm_provider=None)

        assert record.source == "template"
        assert record.prompt_hash is None
        assert record.entry_rationale
        assert record.exit_rationale
        assert record.what_went_right
        assert record.what_went_wrong

        rows = (
            await db_session.execute(
                select(ExplanationRecord).where(ExplanationRecord.trade_id == trade.id)
            )
        ).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_template(self, db_session):
        account = await _seed_account(db_session)
        trade = _manual_trade(account.id)
        db_session.add(trade)
        await db_session.commit()

        record = await generate_explanation_for_trade(
            db_session, trade, llm_provider=FakeFailingProvider()
        )
        assert record.source == "template"
        assert record.prompt_hash is None

    @pytest.mark.asyncio
    async def test_working_llm_provider_is_used_and_hashed(self, db_session):
        account = await _seed_account(db_session)
        trade = _manual_trade(account.id)
        db_session.add(trade)
        await db_session.commit()

        record = await generate_explanation_for_trade(
            db_session, trade, llm_provider=FakeWorkingProvider()
        )
        assert record.source == "llm"
        assert record.prompt_hash is not None
        assert record.entry_rationale == "llm entry"


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_calling_twice_does_not_duplicate_rows(self, db_session):
        account = await _seed_account(db_session)
        trade = _manual_trade(account.id)
        db_session.add(trade)
        await db_session.commit()

        first = await generate_explanation_for_trade(db_session, trade, llm_provider=None)
        second = await generate_explanation_for_trade(db_session, trade, llm_provider=None)
        assert first.id == second.id

        rows = (
            await db_session.execute(
                select(ExplanationRecord).where(ExplanationRecord.trade_id == trade.id)
            )
        ).scalars().all()
        assert len(rows) == 1


class TestManualTradeHonesty:
    @pytest.mark.asyncio
    async def test_manual_trade_says_so_plainly_and_does_not_fabricate_a_rule(self, db_session):
        account = await _seed_account(db_session)
        trade = _manual_trade(account.id)
        db_session.add(trade)
        await db_session.commit()

        record = await generate_explanation_for_trade(db_session, trade, llm_provider=None)

        assert "manual" in record.entry_rationale.lower()
        assert "no recorded strategy evidence" in record.entry_rationale.lower()
        assert "rule_id" not in record.entry_rationale  # never a fabricated field name
        # Outcome is still explained honestly from the real Trade row.
        assert "50.00" in record.what_went_right or "50.00" in record.what_went_wrong


class TestSignalLinkedTrade:
    @pytest.mark.asyncio
    async def test_entry_rationale_cites_the_real_rule_and_conditions(self, db_session):
        account = await _seed_account(db_session)
        strategy = StrategyRecord(
            user_id=account.user_id, slug="vwap_reversion", name="VWAP", params={}
        )
        db_session.add(strategy)
        await db_session.flush()

        entry_signal = SignalRecord(
            strategy_id=strategy.id, account_id=account.id, symbol="XLF",
            ts=NOW - timedelta(minutes=20), side="buy", intent="entry",
            rule_id="vwap_reversion.long_entry",
            rule_text="Enter long when price is >2.0 std below session VWAP",
            features={"vwap_zscore": -2.34},
            conditions=[
                {"name": "vwap_zscore", "description": "price vs VWAP", "operator": "<",
                 "threshold": -2.0, "actual": -2.34, "passed": True},
                {"name": "above_ema200", "description": "above 200 EMA", "operator": ">",
                 "threshold": 0, "actual": 1, "passed": True},
            ],
            confidence=Decimal("0.71"), acted_on=True,
        )
        db_session.add(entry_signal)
        await db_session.flush()

        trade = Trade(
            account_id=account.id, strategy_id=strategy.id, entry_signal_id=entry_signal.id,
            symbol="XLF", side="buy", qty=Decimal("10"),
            entry_price=Decimal("40.00"), exit_price=Decimal("45.00"),
            opened_at=NOW - timedelta(minutes=20), closed_at=NOW,
            gross_pnl=Decimal("50.00"), total_friction=Decimal("2.50"),
            net_pnl=Decimal("47.50"), r_multiple=Decimal("1.0"), exit_reason="target",
        )
        db_session.add(trade)
        await db_session.commit()

        record = await generate_explanation_for_trade(db_session, trade, llm_provider=None)

        assert "vwap_reversion.long_entry" in record.entry_rationale
        assert "-2.34" in record.entry_rationale


class TestNoFabricatedNumbers:
    """CLAUDE.md rule 2 / ADDENDUM_LIVE_APPROVAL §7's golden-file check:
    every number in the rendered explanation traces to the assembled input
    payload -- nothing is invented."""

    @staticmethod
    def _numbers_in(text: str) -> list[float]:
        found = re.findall(r"-?\d[\d,]*\.?\d*", text)
        return [float(n.replace(",", "")) for n in found]

    @staticmethod
    def _flatten_numbers(obj) -> list[float]:
        out: list[float] = []
        if isinstance(obj, bool):
            return out
        if isinstance(obj, (int, float)):
            out.append(float(obj))
        elif isinstance(obj, dict):
            for v in obj.values():
                out.extend(TestNoFabricatedNumbers._flatten_numbers(v))
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                out.extend(TestNoFabricatedNumbers._flatten_numbers(v))
        return out

    @pytest.mark.asyncio
    async def test_every_rendered_number_traces_to_the_payload(self, db_session):
        account = await _seed_account(db_session)
        strategy = StrategyRecord(user_id=account.user_id, slug="orb", name="ORB", params={})
        db_session.add(strategy)
        await db_session.flush()

        entry_signal = SignalRecord(
            strategy_id=strategy.id, account_id=account.id, symbol="AAPL",
            ts=NOW - timedelta(minutes=30), side="buy", intent="entry",
            rule_id="orb.breakout_long", rule_text="Breakout above opening range high",
            features={"orb_high": 231.44, "rel_volume": 1.31},
            conditions=[
                {"name": "above_orb_high", "description": "above ORB high", "operator": ">",
                 "threshold": 231.44, "actual": 231.52, "passed": True},
            ],
            confidence=Decimal("0.55"), acted_on=True,
        )
        exit_signal = SignalRecord(
            strategy_id=strategy.id, account_id=account.id, symbol="AAPL",
            ts=NOW, side="sell", intent="exit",
            rule_id="risk.stop_hit", rule_text="Stop loss triggered",
            features={"atr": 1.2},
            conditions=[
                {"name": "price_at_stop", "description": "price at stop", "operator": "<=",
                 "threshold": 230.10, "actual": 230.05, "passed": True},
            ],
            confidence=None, acted_on=True,
        )
        db_session.add_all([entry_signal, exit_signal])
        await db_session.flush()

        trade = Trade(
            account_id=account.id, strategy_id=strategy.id,
            entry_signal_id=entry_signal.id, exit_signal_id=exit_signal.id,
            symbol="AAPL", side="buy", qty=Decimal("25"),
            entry_price=Decimal("231.52"), exit_price=Decimal("230.05"),
            opened_at=NOW - timedelta(minutes=30), closed_at=NOW,
            gross_pnl=Decimal("-36.75"), total_friction=Decimal("3.10"),
            net_pnl=Decimal("-39.85"), r_multiple=Decimal("-1.02"), exit_reason="stop",
        )
        db_session.add(trade)
        await db_session.commit()

        context = await build_trade_context(db_session, trade)
        payload = assemble_payload(trade, entry_signal, exit_signal, context)
        allowed = self._flatten_numbers(payload)

        record = await generate_explanation_for_trade(db_session, trade, llm_provider=None)
        rendered_text = " ".join([
            record.entry_rationale, record.exit_rationale,
            record.what_went_right, record.what_went_wrong,
        ])

        for n in self._numbers_in(rendered_text):
            assert any(abs(n - a) < 0.02 for a in allowed), (
                f"number {n} in rendered explanation has no matching value in the "
                f"assembled payload: {allowed}"
            )


class TestBuildTradeContext:
    @pytest.mark.asyncio
    async def test_trades_today_and_consecutive_losses(self, db_session):
        account = await _seed_account(db_session)

        t1 = _manual_trade(account.id, net_pnl="-10.00", closed_at=NOW - timedelta(hours=2))
        t2 = _manual_trade(account.id, net_pnl="-20.00", closed_at=NOW - timedelta(hours=1))
        t3 = _manual_trade(account.id, net_pnl="30.00", closed_at=NOW)
        db_session.add_all([t1, t2, t3])
        await db_session.commit()

        ctx = await build_trade_context(db_session, t3)
        assert ctx["trades_today"] == 3
        # t3 itself is a win, so consecutive_losses counts the streak *before*
        # it: t2 and t1 were both losses.
        assert ctx["consecutive_losses"] == 2

        ctx2 = await build_trade_context(db_session, t2)
        assert ctx2["consecutive_losses"] == 1

    @pytest.mark.asyncio
    async def test_manual_trade_has_no_signal_evidence(self, db_session):
        account = await _seed_account(db_session)
        trade = _manual_trade(account.id)
        db_session.add(trade)
        await db_session.commit()

        ctx = await build_trade_context(db_session, trade)
        assert ctx["has_signal_evidence"] is False
        assert ctx["followed_rules"] is None
        assert ctx["source"] == "manual"
