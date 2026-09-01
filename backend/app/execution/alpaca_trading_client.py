"""Real Alpaca paper-trading adapter. BUILD_SPEC §7.5, §15.

Implements the narrow `AlpacaTradingClient` protocol `PaperBroker` depends
on, backed by alpaca-py's (synchronous) `TradingClient`. Each call is
offloaded via `asyncio.to_thread` so it doesn't block the event loop the rest
of the app runs on.

Never constructed against anything but the paper endpoint —
`app.config.Settings` already refuses to boot otherwise (CLAUDE.md rule 1);
the assertion below means this specific class also can't be pointed anywhere
else, even if some future caller assembled a `Settings` object by hand.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.config import Settings
from app.execution.broker import BrokerAccount, BrokerPosition
from app.execution.paper_broker import RawAlpacaFill


class AlpacaPaperTradingClient:
    def __init__(self, settings: Settings):
        if "paper-api" not in settings.alpaca_paper_base_url:
            raise RuntimeError(
                "AlpacaPaperTradingClient requires a paper-api base URL "
                f"(got {settings.alpaca_paper_base_url!r})"
            )

        from alpaca.trading.client import TradingClient

        self._client = TradingClient(
            settings.alpaca_api_key, settings.alpaca_api_secret, paper=True,
        )

    async def submit_market_order(self, symbol: str, side: str, qty: Decimal) -> RawAlpacaFill:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = await asyncio.to_thread(self._client.submit_order, request)

        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        return RawAlpacaFill(
            broker_order_id=str(order.id),
            status=status,
            filled_qty=Decimal(str(order.filled_qty or 0)),
            filled_avg_price=Decimal(str(order.filled_avg_price or 0)),
            submitted_at=order.submitted_at or datetime.now(UTC),
            filled_at=order.filled_at,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        await asyncio.to_thread(self._client.cancel_order_by_id, broker_order_id)

    async def get_positions(self) -> list[BrokerPosition]:
        positions = await asyncio.to_thread(self._client.get_all_positions)
        return [
            BrokerPosition(
                symbol=p.symbol, qty=Decimal(str(p.qty)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
            )
            for p in positions
        ]

    async def get_account(self) -> BrokerAccount:
        account = await asyncio.to_thread(self._client.get_account)
        return BrokerAccount(cash=Decimal(str(account.cash)), equity=Decimal(str(account.equity)))
