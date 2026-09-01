"""The only Broker implementation. BUILD_SPEC §7.5, CLAUDE.md rule 1.

Alpaca paper gives realistic order lifecycle, market-hours behavior, and
position bookkeeping for free — but per Alpaca's own documentation it does
not simulate market impact, latency slippage, queue position, borrow fees,
dividends, or regulatory fees, and it fills at the quoted bid/ask without
validating size against real liquidity. Left alone that's an optimistically
biased simulator, so every fill this class produces is re-priced through the
friction model (§9) before it is handed back to the rest of the system.

The raw Alpaca fill is still recorded (`RawAlpacaFill`) for audit purposes —
it becomes the calibration baseline in the live-mode reconciliation work
(ADDENDUM_LIVE_APPROVAL §6) — but it is never used for P&L math.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.execution.broker import (
    Broker,
    BrokerAccount,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from app.execution.friction import FrictionConfig, FrictionInput, apply_friction


@dataclass(frozen=True)
class RawAlpacaFill:
    """What the underlying Alpaca paper endpoint actually returned."""

    broker_order_id: str
    status: str
    filled_qty: Decimal
    filled_avg_price: Decimal
    submitted_at: datetime
    filled_at: datetime | None


class AlpacaTradingClient(Protocol):
    """Narrow structural type over alpaca-py's TradingClient.

    Kept separate from alpaca-py's own classes — same reason as
    `MessageSource` in app/ingest/alpaca_source.py — so tests can supply a
    fake without network access or paper credentials.
    """

    async def submit_market_order(self, symbol: str, side: str, qty: Decimal) -> RawAlpacaFill: ...
    async def cancel_order(self, broker_order_id: str) -> None: ...
    async def get_positions(self) -> list[BrokerPosition]: ...
    async def get_account(self) -> BrokerAccount: ...


@dataclass(frozen=True)
class MarketSnapshot:
    """The quote/ATR/volume context the friction model needs for one symbol."""

    bid: Decimal
    ask: Decimal
    atr: Decimal
    typical_bar_volume: Decimal


class MarketSnapshotProvider(Protocol):
    async def snapshot(self, symbol: str) -> MarketSnapshot: ...


class PaperBroker(Broker):
    def __init__(
        self,
        alpaca_client: AlpacaTradingClient,
        market_data: MarketSnapshotProvider,
        friction_cfg: FrictionConfig | None = None,
    ):
        self._client = alpaca_client
        self._market = market_data
        self._friction_cfg = friction_cfg or FrictionConfig()

    async def submit(self, order: OrderRequest) -> BrokerOrder:
        raw = await self._client.submit_market_order(order.symbol, order.side, order.qty)

        fill = None
        if raw.status in ("filled", "partial") and raw.filled_qty > 0:
            snap = await self._market.snapshot(order.symbol)
            fill = apply_friction(
                FrictionInput(
                    side=order.side,
                    qty=raw.filled_qty,
                    ts=raw.filled_at or raw.submitted_at,
                    bid=snap.bid,
                    ask=snap.ask,
                    atr=snap.atr,
                    typical_bar_volume=snap.typical_bar_volume,
                ),
                self._friction_cfg,
            )

        return BrokerOrder(
            broker_order_id=raw.broker_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            status=raw.status,
            submitted_at=raw.submitted_at,
            filled_at=raw.filled_at,
            fill=fill,
        )

    async def cancel(self, broker_order_id: str) -> None:
        await self._client.cancel_order(broker_order_id)

    async def get_positions(self) -> list[BrokerPosition]:
        return await self._client.get_positions()

    async def get_account(self) -> BrokerAccount:
        return await self._client.get_account()
