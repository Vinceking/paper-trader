"""`GET /signals` — the recommendation feed. BUILD_SPEC §14: "includes vetoed
signals".

A plain, server-side-scoped read of every `SignalRecord` ever written for
the current user's own account — acted on or not, vetoed or not. This is
deliberately unfiltered by a strategy's `enabled`/`gate_passed` state: a
signal is evidence that a rule fired against real (or replayed) market data,
not a claim that anything was, or should have been, executed on it (see
`app/ingest/pipeline.py`'s hard boundary — nothing upstream of this table
ever places an order). Scoped via `CurrentUserAccount`, the same pattern
`app/api/routes_account.py` already uses — never a client-supplied account id.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import CurrentUserAccount, DbSession
from app.models.signals import SignalRecord
from app.models.strategies import StrategyRecord
from app.schemas.signals import ConditionOut, SignalOut

router = APIRouter(prefix="/signals", tags=["signals"])

# Mirrors the convention in app/api/routes_account.py's GET /account/trades.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@router.get("", response_model=list[SignalOut])
async def list_signals(
    account: CurrentUserAccount,
    db: DbSession,
    acted_on: bool | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[SignalOut]:
    query = (
        select(SignalRecord, StrategyRecord)
        .join(StrategyRecord, StrategyRecord.id == SignalRecord.strategy_id)
        .where(SignalRecord.account_id == account.id)
        .order_by(SignalRecord.ts.desc())
        .limit(limit)
    )
    if acted_on is not None:
        query = query.where(SignalRecord.acted_on == acted_on)

    rows = (await db.execute(query)).all()
    return [
        SignalOut(
            id=signal.id, symbol=signal.symbol, ts=signal.ts, side=signal.side,
            intent=signal.intent, rule_id=signal.rule_id, rule_text=signal.rule_text,
            features=signal.features,
            conditions=[ConditionOut(**c) for c in signal.conditions],
            confidence=signal.confidence, acted_on=signal.acted_on,
            veto_reason=signal.veto_reason,
            strategy_id=strategy.id, strategy_slug=strategy.slug, strategy_name=strategy.name,
        )
        for signal, strategy in rows
    ]
