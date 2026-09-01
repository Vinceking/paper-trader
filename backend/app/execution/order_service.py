"""Manual order orchestration. BUILD_SPEC Phase 2: risk evaluation -> broker
submission -> orders/fills/positions/trades persistence.

BUILD_SPEC's Phase 2 "Build:" line treats "manual order endpoint" as one
deliverable; this module is the actual orchestration, kept separate from the
FastAPI route (app/api/routes_orders.py) so it can be exercised directly in
tests without a running HTTP server.

Scope note: `paper_accounts.cash`/`equity` are deliberately NOT mutated here.
Tracking live mark-to-market equity requires the indicator/quote pipeline
that doesn't exist until Phase 3, and no phase has assigned that bookkeeping
yet. Instead, "today's realized P&L" (which is what the daily-loss veto
actually needs) is derived directly from querying `trades` rows for the
current trading day. This keeps Phase 2 self-contained without inventing an
equity-tracking subsystem the spec hasn't asked for yet.

Scope note 2: the reference quote (bid/ask/atr/typical_bar_volume) is
supplied by the caller rather than fetched from a live source. Phase 1 built
the market-data *pipeline* (ingest, bar building) but never wired it to
Postgres/Redis, and wiring a live snapshot provider is Phase 3's indicator
pipeline's job, not Phase 2's. In a real deployment the frontend already has
a live quote on screen when the user places a manual order (that's what the
3-second hold-to-confirm in §13.4 is confirming against), so passing it
through is architecturally reasonable for now — full server-side
verification against an independent quote is a hardening item, not a Phase 2
requirement.

That same client-supplied quote is used for *both* the risk engine's entry
price and PaperBroker's friction pricing (via a throwaway
`MarketSnapshotProvider` built from it) — sizing a trade against one price
and filling it against another would be internally inconsistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.broker import OrderRequest
from app.execution.friction import FrictionConfig, mid_price
from app.execution.paper_broker import AlpacaTradingClient, MarketSnapshot, PaperBroker
from app.execution.positions import OpenPosition, close_position
from app.market_calendar import minutes_until_session_close, start_of_trading_day
from app.models.account import PaperAccount
from app.models.orders import Fill, Order
from app.models.positions import Position, Trade
from app.models.risk import RiskEvent, RiskSettings
from app.risk.engine import AccountState, RiskDecision, RiskEngine, RiskSettingsInput, RiskSignal

# Mirrors the risk_settings table's own column defaults (BUILD_SPEC §5), used
# when an account has no risk_settings row yet.
DEFAULT_RISK_SETTINGS = RiskSettingsInput(
    risk_per_trade_pct=Decimal("0.01"),
    max_daily_loss_pct=Decimal("0.03"),
    max_open_positions=3,
    max_trades_per_day=10,
    max_position_pct=Decimal("0.20"),
    cooldown_after_losses=3,
    cooldown_minutes=30,
)

# Only full closes are supported in Phase 2 — partial exits would need
# partial-fill bookkeeping on Position that nothing here builds yet.
_RECENT_TRADES_SCAN_LIMIT = 20


class UnknownAccountError(Exception):
    pass


class RiskVetoError(Exception):
    def __init__(self, decision: RiskDecision):
        self.decision = decision
        super().__init__(decision.veto_reason)


class NoOpenPositionError(Exception):
    def __init__(self, symbol: str):
        self.symbol = symbol
        super().__init__(f"no open position in {symbol}")


@dataclass(frozen=True)
class ReferenceQuote:
    bid: Decimal
    ask: Decimal
    atr: Decimal
    typical_bar_volume: Decimal


@dataclass(frozen=True)
class ManualOrderRequest:
    account_id: UUID
    symbol: str
    side: str  # 'buy' | 'sell'
    intent: str  # 'entry' | 'exit'
    quote: ReferenceQuote
    stop_price: Decimal | None = None
    target_price: Decimal | None = None


@dataclass(frozen=True)
class ManualOrderResult:
    order: Order
    fill: Fill | None
    position: Position | None
    trade: Trade | None


async def _load_risk_settings(db: AsyncSession, account_id: UUID) -> RiskSettingsInput:
    row = await db.get(RiskSettings, account_id)
    if row is None:
        return DEFAULT_RISK_SETTINGS
    return RiskSettingsInput(
        risk_per_trade_pct=row.risk_per_trade_pct,
        max_daily_loss_pct=row.max_daily_loss_pct,
        max_open_positions=row.max_open_positions,
        max_trades_per_day=row.max_trades_per_day,
        max_position_pct=row.max_position_pct,
        cooldown_after_losses=row.cooldown_after_losses,
        cooldown_minutes=row.cooldown_minutes,
    )


async def _account_state(
    db: AsyncSession, account_id: UUID, equity: Decimal, now: datetime,
) -> AccountState:
    day_start = start_of_trading_day(now)

    todays_trades = (
        await db.execute(
            select(Trade).where(Trade.account_id == account_id, Trade.closed_at >= day_start)
        )
    ).scalars().all()
    realized_pnl_today = sum((t.net_pnl for t in todays_trades), Decimal(0))

    open_positions = (
        await db.execute(
            select(Position).where(Position.account_id == account_id, Position.status == "open")
        )
    ).scalars().all()

    recent_trades = (
        await db.execute(
            select(Trade)
            .where(Trade.account_id == account_id)
            .order_by(Trade.closed_at.desc())
            .limit(_RECENT_TRADES_SCAN_LIMIT)
        )
    ).scalars().all()
    consecutive_losses = 0
    last_loss_at = None
    for t in recent_trades:
        if t.net_pnl >= 0:
            break
        consecutive_losses += 1
        if last_loss_at is None:
            last_loss_at = t.closed_at

    return AccountState(
        equity=equity,
        starting_equity_today=equity - realized_pnl_today,
        realized_pnl_today=realized_pnl_today,
        open_positions_count=len(open_positions),
        trades_today_count=len(todays_trades),
        consecutive_losses=consecutive_losses,
        last_loss_at=last_loss_at,
        now=now,
        minutes_until_close=minutes_until_session_close(now),
    )


def _fill_friction_total(fill: Fill) -> Decimal:
    return fill.slippage_cost + fill.spread_cost + fill.commission + fill.reg_fees


@dataclass(frozen=True)
class _StaticSnapshotProvider:
    """Wraps one request's client-supplied quote as a MarketSnapshotProvider.

    Built fresh per request so PaperBroker's friction pricing always uses the
    exact same quote the risk engine sized against — see the module
    docstring's scope note.
    """

    value: MarketSnapshot

    async def snapshot(self, symbol: str) -> MarketSnapshot:
        return self.value


async def submit_manual_order(
    db: AsyncSession,
    alpaca_client: AlpacaTradingClient,
    request: ManualOrderRequest,
    now: datetime,
    friction_cfg: FrictionConfig | None = None,
) -> ManualOrderResult:
    account = await db.get(PaperAccount, request.account_id)
    if account is None:
        raise UnknownAccountError(str(request.account_id))

    settings = await _load_risk_settings(db, request.account_id)
    state = await _account_state(db, request.account_id, account.equity, now)

    entry_price = mid_price(request.quote.bid, request.quote.ask)
    signal = RiskSignal(
        symbol=request.symbol,
        side=request.side,
        intent=request.intent,
        entry_price=entry_price,
        stop_price=request.stop_price,
    )
    decision = RiskEngine().evaluate(signal, state, settings)

    if not decision.approved:
        db.add(RiskEvent(
            account_id=request.account_id,
            event_type="veto",
            detail={"reason": decision.veto_reason, **decision.detail},
        ))
        await db.commit()
        raise RiskVetoError(decision)

    position_to_close: Position | None = None
    if request.intent == "exit":
        position_to_close = (
            await db.execute(
                select(Position).where(
                    Position.account_id == request.account_id,
                    Position.symbol == request.symbol,
                    Position.status == "open",
                )
            )
        ).scalar_one_or_none()
        if position_to_close is None:
            raise NoOpenPositionError(request.symbol)
        qty = position_to_close.qty
    else:
        assert decision.qty is not None  # guaranteed by an approved entry decision
        qty = decision.qty

    snapshot = MarketSnapshot(
        bid=request.quote.bid,
        ask=request.quote.ask,
        atr=request.quote.atr,
        typical_bar_volume=request.quote.typical_bar_volume,
    )
    broker = PaperBroker(alpaca_client, _StaticSnapshotProvider(snapshot), friction_cfg)
    broker_order = await broker.submit(
        OrderRequest(symbol=request.symbol, side=request.side, qty=qty)
    )

    order = Order(
        account_id=request.account_id,
        symbol=request.symbol,
        side=request.side,
        qty=qty,
        order_type="market",
        stop_price=request.stop_price,
        status=broker_order.status,
        source="manual",
        broker_order_id=broker_order.broker_order_id,
        submitted_at=broker_order.submitted_at,
    )
    db.add(order)
    await db.flush()  # assigns order.id for FKs below

    fill_row: Fill | None = None
    if broker_order.fill is not None:
        f = broker_order.fill
        fill_row = Fill(
            order_id=order.id,
            qty=f.qty,
            reference_price=f.reference_price,
            fill_price=f.fill_price,
            slippage_cost=f.slippage_cost,
            spread_cost=f.spread_cost,
            commission=f.commission,
            reg_fees=f.reg_fees,
            filled_at=broker_order.filled_at or now,
        )
        db.add(fill_row)

    position_row: Position | None = None
    trade_row: Trade | None = None

    if fill_row is not None and request.intent == "entry":
        position_row = Position(
            account_id=request.account_id,
            entry_order_id=order.id,
            symbol=request.symbol,
            qty=fill_row.qty,
            avg_entry_price=fill_row.fill_price,
            stop_price=request.stop_price,
            target_price=request.target_price,
            opened_at=broker_order.filled_at or now,
            status="open",
        )
        db.add(position_row)

    elif fill_row is not None and position_to_close is not None:
        entry_fill = (
            await db.execute(select(Fill).where(Fill.order_id == position_to_close.entry_order_id))
        ).scalar_one()

        closed = close_position(
            OpenPosition(
                symbol=position_to_close.symbol,
                side="buy",  # this build only ever opens long positions — §0.3
                qty=position_to_close.qty,
                avg_entry_price=position_to_close.avg_entry_price,
                stop_price=position_to_close.stop_price,
                opened_at=position_to_close.opened_at,
            ),
            exit_price=fill_row.fill_price,
            exit_qty=fill_row.qty,
            entry_friction=_fill_friction_total(entry_fill),
            exit_friction=_fill_friction_total(fill_row),
            closed_at=broker_order.filled_at or now,
            exit_reason="manual",
        )
        trade_row = Trade(
            account_id=request.account_id,
            symbol=closed.symbol,
            side=closed.side,
            qty=closed.qty,
            entry_price=closed.entry_price,
            exit_price=closed.exit_price,
            opened_at=closed.opened_at,
            closed_at=closed.closed_at,
            gross_pnl=closed.gross_pnl,
            total_friction=closed.total_friction,
            net_pnl=closed.net_pnl,
            r_multiple=closed.r_multiple,
            exit_reason=closed.exit_reason,
        )
        db.add(trade_row)
        position_to_close.status = "closed"

    await db.commit()

    return ManualOrderResult(order=order, fill=fill_row, position=position_row, trade=trade_row)
