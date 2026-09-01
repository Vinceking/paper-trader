"""Manual order endpoint. BUILD_SPEC §14, Phase 2.

Deliberately thin — the orchestration (risk evaluation, broker submission,
persistence) lives in app/execution/order_service.py so it can be exercised
directly in tests without a running HTTP server.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.deps import AlpacaClient, Clock, DbSession
from app.execution.order_service import (
    ManualOrderRequest,
    NoOpenPositionError,
    ReferenceQuote,
    RiskVetoError,
    UnknownAccountError,
    submit_manual_order,
)
from app.schemas.orders import FillOut, ManualOrderIn, ManualOrderOut, TradeOut

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", response_model=ManualOrderOut, status_code=201)
async def create_order(
    body: ManualOrderIn, db: DbSession, alpaca_client: AlpacaClient, now: Clock,
) -> ManualOrderOut:
    request = ManualOrderRequest(
        account_id=body.account_id,
        symbol=body.symbol,
        side=body.side,
        intent=body.intent,
        quote=ReferenceQuote(
            bid=body.quote.bid, ask=body.quote.ask,
            atr=body.quote.atr, typical_bar_volume=body.quote.typical_bar_volume,
        ),
        stop_price=body.stop_price,
        target_price=body.target_price,
    )

    try:
        result = await submit_manual_order(db, alpaca_client, request, now)
    except UnknownAccountError as exc:
        raise HTTPException(status_code=404, detail=f"unknown account {exc}") from exc
    except NoOpenPositionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RiskVetoError as exc:
        raise HTTPException(
            status_code=409,
            detail={"veto_reason": exc.decision.veto_reason, **exc.decision.detail},
        ) from exc

    fill_out = None
    if result.fill is not None:
        f = result.fill
        fill_out = FillOut(
            qty=f.qty, reference_price=f.reference_price, fill_price=f.fill_price,
            slippage_cost=f.slippage_cost, spread_cost=f.spread_cost,
            commission=f.commission, reg_fees=f.reg_fees,
        )

    trade_out = None
    if result.trade is not None:
        t = result.trade
        trade_out = TradeOut(
            id=t.id, gross_pnl=t.gross_pnl, total_friction=t.total_friction,
            net_pnl=t.net_pnl, r_multiple=t.r_multiple, exit_reason=t.exit_reason,
        )

    return ManualOrderOut(
        order_id=result.order.id,
        status=result.order.status,
        fill=fill_out,
        position_id=result.position.id if result.position else None,
        trade=trade_out,
    )
