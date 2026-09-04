"""Signal persistence tests. CLAUDE.md rule 2: written before any order."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.account import PaperAccount
from app.models.signals import SignalRecord
from app.models.strategies import StrategyRecord
from app.strategies.base import Condition, Signal
from app.strategies.signal_service import persist_signal

NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)


def make_signal() -> Signal:
    return Signal(
        side="buy", intent="entry", symbol="XLF",
        rule_id="rsi2.oversold_long", rule_text="RSI(2) < 10 and price > SMA200",
        features={"rsi_2": 7.3, "sma_200": Decimal("48.20"), "regime": "trend_up"},
        conditions=[
            Condition("rsi_below_threshold", "RSI(2) below oversold", "<", 10.0, 7.3, True),
            Condition("above_sma200", "price above 200-SMA", ">", 48.0, 47.9, False),
        ],
        stop_price=Decimal("47.00"),
        target_price=Decimal("50.00"),
        confidence=0.62,
    )


@pytest.mark.asyncio
async def test_persist_signal_writes_full_evidence(db_session):
    account = PaperAccount(
        id=uuid4(), user_id=uuid4(), name="test",
        starting_cash=Decimal("100000"), cash=Decimal("100000"), equity=Decimal("100000"),
    )
    strategy = StrategyRecord(
        id=uuid4(), user_id=account.user_id, slug="rsi2", name="RSI(2) mean reversion",
        params={}, enabled=False, gate_passed=False,
    )
    db_session.add_all([account, strategy])
    await db_session.flush()

    signal = make_signal()
    record = await persist_signal(db_session, signal, strategy.id, account.id, NOW)
    await db_session.commit()

    assert record.id is not None
    assert record.acted_on is False
    assert record.veto_reason is None

    loaded = (await db_session.execute(
        select(SignalRecord).where(SignalRecord.id == record.id)
    )).scalar_one()

    assert loaded.rule_id == "rsi2.oversold_long"
    assert loaded.side == "buy"
    assert loaded.intent == "entry"
    assert loaded.ts == NOW
    # Every condition survives the round trip, including the failed one.
    assert len(loaded.conditions) == 2
    assert loaded.conditions[1]["passed"] is False
    assert loaded.conditions[0]["actual"] == 7.3
    # Decimal features are stringified for JSON, not silently dropped.
    assert loaded.features["sma_200"] == "48.20"
    assert loaded.features["regime"] == "trend_up"
