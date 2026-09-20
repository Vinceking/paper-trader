"""End-to-end: closing a trade through `POST /orders` produces an
`ExplanationRecord`, and `GET /account/trades` surfaces it. Phase 5.

This is the one test in the suite that goes through the real HTTP call
chain (register -> place entry -> place exit -> read trades) rather than
calling `app/education/explainer.py` directly, so it's the actual proof
that `app/execution/order_service.py`'s wiring (added this phase) works,
not just that the explainer module works in isolation. Same harness
pattern as tests/test_orders_api.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.deps import get_alpaca_client, get_now
from app.execution.broker import BrokerAccount, BrokerPosition
from app.execution.paper_broker import RawAlpacaFill
from app.main import create_app
from app.models import Base

QUOTE = {"bid": "99.90", "ask": "100.10", "atr": "1.00", "typical_bar_volume": "10000"}
FIXED_NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)  # 13:00 ET


class FakeAlpacaClient:
    async def submit_market_order(self, symbol: str, side: str, qty: Decimal) -> RawAlpacaFill:
        now = datetime.now(UTC)
        return RawAlpacaFill(
            broker_order_id=f"fake-{uuid4()}", status="filled",
            filled_qty=qty, filled_avg_price=Decimal("0"),
            submitted_at=now, filled_at=now,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        pass

    async def get_positions(self) -> list[BrokerPosition]:
        return []

    async def get_account(self) -> BrokerAccount:
        return BrokerAccount(cash=Decimal("100000"), equity=Decimal("100000"))


@dataclass
class Harness:
    client: TestClient


@pytest_asyncio.fixture
async def harness():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_alpaca_client] = lambda: FakeAlpacaClient()
    app.dependency_overrides[get_now] = lambda: FIXED_NOW

    with TestClient(app) as client:
        yield Harness(client=client)

    await engine.dispose()


def _register(client: TestClient) -> dict:
    resp = client.post(
        "/auth/register",
        json={"email": "trader@example.com", "password": "hunter2pass", "display_name": "Trader"},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestExplanationSurfacedOnClose:
    @pytest.mark.asyncio
    async def test_closing_a_trade_produces_a_persisted_explanation(self, harness):
        headers = _register(harness.client)
        account = harness.client.get("/account", headers=headers).json()
        account_id = account["id"]

        entry_body = {
            "account_id": account_id, "symbol": "XLF", "side": "buy", "intent": "entry",
            "quote": QUOTE, "stop_price": "98.00", "confirm_token": "held-for-3-seconds",
        }
        entry_resp = harness.client.post("/orders", json=entry_body)
        assert entry_resp.status_code == 201, entry_resp.text

        exit_body = {
            "account_id": account_id, "symbol": "XLF", "side": "sell", "intent": "exit",
            "quote": QUOTE, "confirm_token": "held-for-3-seconds",
        }
        exit_resp = harness.client.post("/orders", json=exit_body)
        assert exit_resp.status_code == 201, exit_resp.text

        trades = harness.client.get("/account/trades", headers=headers).json()
        assert len(trades) == 1
        trade = trades[0]

        assert trade["explanation"] is not None
        explanation = trade["explanation"]
        assert explanation["source"] == "template"  # no XAI_API_KEY tonight — see .env
        assert explanation["entry_rationale"]
        assert explanation["exit_rationale"]
        assert explanation["what_went_right"]
        assert explanation["what_went_wrong"]
        # This flow never links a SignalRecord (manual order path) — the
        # explanation must say so honestly rather than invent one.
        assert "manual" in explanation["entry_rationale"].lower()
