"""Signal persistence. CLAUDE.md rule 2, BUILD_SPEC §11.1.

The `signals` row is written *before* any order is submitted — the
anti-hallucination guarantee the whole education layer depends on later.
This module has exactly one job: turn a domain `Signal` (app.strategies.base)
into a `SignalRecord` row. It never touches a broker, and nothing about it
depends on whether the signal is ever acted on.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.signals import SignalRecord
from app.strategies.base import Signal


def _jsonable(value):
    """JSONB can't store Decimal directly — stringify it, same convention
    risk_events already uses for its `detail` column."""
    if isinstance(value, Decimal):
        return str(value)
    return value


async def persist_signal(
    db: AsyncSession,
    signal: Signal,
    strategy_id: UUID,
    account_id: UUID,
    ts: datetime,
) -> SignalRecord:
    record = SignalRecord(
        strategy_id=strategy_id,
        account_id=account_id,
        symbol=signal.symbol,
        ts=ts,
        side=signal.side,
        intent=signal.intent,
        rule_id=signal.rule_id,
        rule_text=signal.rule_text,
        features={k: _jsonable(v) for k, v in signal.features.items()},
        conditions=[
            {
                "name": c.name,
                "description": c.description,
                "operator": c.operator,
                "threshold": c.threshold,
                "actual": c.actual,
                "passed": c.passed,
            }
            for c in signal.conditions
        ],
        confidence=signal.confidence,
        acted_on=False,
    )
    db.add(record)
    await db.flush()
    return record
