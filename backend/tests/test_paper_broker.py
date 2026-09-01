"""PaperBroker tests. BUILD_SPEC §7.5.

Uses fake Alpaca/market-data clients — no network access, no paper API keys
needed — so the assertions can focus on the one thing that matters: the raw
Alpaca fill is discarded for pricing purposes and replaced by the
friction-adjusted price (§9). Numbers mirror test_friction.py's hand-computed
scenarios so both are cross-checked against each other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.execution.broker import BrokerAccount, BrokerPosition, OrderRequest
from app.execution.friction import FrictionConfig
from app.execution.paper_broker import MarketSnapshot, PaperBroker, RawAlpacaFill

MID_SESSION = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)  # 13:00 ET


class FakeAlpacaClient:
    def __init__(self, raw: RawAlpacaFill):
        self.raw = raw
        self.cancelled: list[str] = []
        self.positions_response: list[BrokerPosition] = []
        self.account_response = BrokerAccount(cash=Decimal("100000"), equity=Decimal("100000"))

    async def submit_market_order(self, symbol: str, side: str, qty: Decimal) -> RawAlpacaFill:
        return self.raw

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)

    async def get_positions(self) -> list[BrokerPosition]:
        return self.positions_response

    async def get_account(self) -> BrokerAccount:
        return self.account_response


class FakeMarketData:
    def __init__(self, snapshot: MarketSnapshot):
        self._snapshot = snapshot

    async def snapshot(self, symbol: str) -> MarketSnapshot:
        return self._snapshot


def make_broker(
    raw: RawAlpacaFill, snapshot: MarketSnapshot,
) -> tuple[PaperBroker, FakeAlpacaClient]:
    client = FakeAlpacaClient(raw)
    broker = PaperBroker(client, FakeMarketData(snapshot), FrictionConfig())
    return broker, client


class TestSubmit:
    @pytest.mark.asyncio
    async def test_buy_fill_price_is_friction_adjusted_not_alpacas_raw_price(self):
        raw = RawAlpacaFill(
            broker_order_id="alpaca-123", status="filled",
            filled_qty=Decimal("100"), filled_avg_price=Decimal("100.00"),  # Alpaca's naive price
            submitted_at=MID_SESSION, filled_at=MID_SESSION,
        )
        snapshot = MarketSnapshot(
            bid=Decimal("99.90"), ask=Decimal("100.10"),
            atr=Decimal("1.00"), typical_bar_volume=Decimal("10000"),
        )
        broker, client = make_broker(raw, snapshot)

        result = await broker.submit(OrderRequest(symbol="XLF", side="buy", qty=Decimal("100")))

        assert result.broker_order_id == "alpaca-123"
        assert result.status == "filled"
        assert result.fill is not None
        # Not Alpaca's 100.00 — the friction-adjusted price from test_friction.py.
        assert result.fill.fill_price == Decimal("100.1755")
        assert result.fill.total_friction == Decimal("17.55")
        assert result.fill.reg_fees == Decimal("0")

    @pytest.mark.asyncio
    async def test_sell_fill_includes_regulatory_fees(self):
        raw = RawAlpacaFill(
            broker_order_id="alpaca-456", status="filled",
            filled_qty=Decimal("200"), filled_avg_price=Decimal("50.00"),
            submitted_at=MID_SESSION, filled_at=MID_SESSION,
        )
        snapshot = MarketSnapshot(
            bid=Decimal("49.98"), ask=Decimal("50.02"),
            atr=Decimal("0.50"), typical_bar_volume=Decimal("2000"),
        )
        broker, _ = make_broker(raw, snapshot)

        result = await broker.submit(OrderRequest(symbol="KRE", side="sell", qty=Decimal("200")))

        assert result.fill.fill_price == Decimal("49.955")
        assert result.fill.reg_fees == Decimal("0.2390146")
        assert result.fill.total_friction == Decimal("9.2390146")

    @pytest.mark.asyncio
    async def test_rejected_order_has_no_fill(self):
        raw = RawAlpacaFill(
            broker_order_id="alpaca-789", status="rejected",
            filled_qty=Decimal("0"), filled_avg_price=Decimal("0"),
            submitted_at=MID_SESSION, filled_at=None,
        )
        snapshot = MarketSnapshot(
            bid=Decimal("10.00"), ask=Decimal("10.02"),
            atr=Decimal("0.10"), typical_bar_volume=Decimal("1000"),
        )
        broker, _ = make_broker(raw, snapshot)

        result = await broker.submit(OrderRequest(symbol="XLE", side="buy", qty=Decimal("50")))

        assert result.status == "rejected"
        assert result.fill is None


class TestDelegation:
    @pytest.mark.asyncio
    async def test_cancel_delegates_to_alpaca_client(self):
        raw = RawAlpacaFill("id", "filled", Decimal("1"), Decimal("1"), MID_SESSION, MID_SESSION)
        snapshot = MarketSnapshot(Decimal("1"), Decimal("1.02"), Decimal("0.1"), Decimal("100"))
        broker, client = make_broker(raw, snapshot)

        await broker.cancel("alpaca-123")
        assert client.cancelled == ["alpaca-123"]

    @pytest.mark.asyncio
    async def test_get_positions_delegates_to_alpaca_client(self):
        raw = RawAlpacaFill("id", "filled", Decimal("1"), Decimal("1"), MID_SESSION, MID_SESSION)
        snapshot = MarketSnapshot(Decimal("1"), Decimal("1.02"), Decimal("0.1"), Decimal("100"))
        broker, client = make_broker(raw, snapshot)
        client.positions_response = [BrokerPosition("SPY", Decimal("10"), Decimal("500.00"))]

        positions = await broker.get_positions()
        assert positions == client.positions_response

    @pytest.mark.asyncio
    async def test_get_account_delegates_to_alpaca_client(self):
        raw = RawAlpacaFill("id", "filled", Decimal("1"), Decimal("1"), MID_SESSION, MID_SESSION)
        snapshot = MarketSnapshot(Decimal("1"), Decimal("1.02"), Decimal("0.1"), Decimal("100"))
        broker, client = make_broker(raw, snapshot)

        account = await broker.get_account()
        assert account == client.account_response
