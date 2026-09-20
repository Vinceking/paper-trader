"""Account/positions/trades read endpoints. BUILD_SPEC §14.

All routes require `get_current_user_account` (via `CurrentUserAccount`), so a
user only ever sees their own paper account's data — enforced server-side by
scoping every query to `account.id`, not merely by hiding UI. This matters:
this service is reachable from the public internet tonight.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import CurrentUserAccount, DbSession
from app.models.explanations import ExplanationRecord
from app.models.positions import Position, Trade
from app.schemas.account import AccountOut, ExplanationOut, PositionOut, TradeOut

router = APIRouter(prefix="/account", tags=["account"])


@router.get("", response_model=AccountOut)
async def get_account(account: CurrentUserAccount) -> AccountOut:
    return AccountOut(
        id=account.id,
        name=account.name,
        cash=account.cash,
        equity=account.equity,
        starting_cash=account.starting_cash,
        benchmark_symbol=account.benchmark_symbol,
    )


@router.get("/positions", response_model=list[PositionOut])
async def get_positions(account: CurrentUserAccount, db: DbSession) -> list[PositionOut]:
    rows = (
        await db.execute(
            select(Position)
            .where(Position.account_id == account.id, Position.status == "open")
            .order_by(Position.opened_at.desc())
        )
    ).scalars().all()
    return [
        PositionOut(
            id=p.id, symbol=p.symbol, qty=p.qty, avg_entry_price=p.avg_entry_price,
            stop_price=p.stop_price, target_price=p.target_price,
            opened_at=p.opened_at, status=p.status,
        )
        for p in rows
    ]


@router.get("/trades", response_model=list[TradeOut])
async def get_trades(
    account: CurrentUserAccount,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TradeOut]:
    # Outer join, not a second round trip -- same pattern as GET /signals'
    # join in routes_signals.py. Outer (not inner) because a trade that
    # predates Phase 5 has no ExplanationRecord at all.
    rows = (
        await db.execute(
            select(Trade, ExplanationRecord)
            .outerjoin(ExplanationRecord, ExplanationRecord.trade_id == Trade.id)
            .where(Trade.account_id == account.id)
            .order_by(Trade.closed_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        TradeOut(
            id=t.id, symbol=t.symbol, side=t.side, qty=t.qty,
            entry_price=t.entry_price, exit_price=t.exit_price,
            opened_at=t.opened_at, closed_at=t.closed_at,
            gross_pnl=t.gross_pnl, total_friction=t.total_friction,
            net_pnl=t.net_pnl, r_multiple=t.r_multiple, exit_reason=t.exit_reason,
            explanation=(
                ExplanationOut(
                    entry_rationale=e.entry_rationale, exit_rationale=e.exit_rationale,
                    what_went_right=e.what_went_right, what_went_wrong=e.what_went_wrong,
                    tip_id=e.tip_id, tip_body=e.tip_body, source=e.source,
                )
                if e is not None else None
            ),
        )
        for t, e in rows
    ]
