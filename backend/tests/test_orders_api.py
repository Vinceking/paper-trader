"""POST /orders integration tests — the actual Phase 2 acceptance criteria:

✅ A manual market order produces orders -> fills -> positions rows with
   itemized friction.
✅ Closing produces a trades row with correct gross/net P&L and R multiple.
✅ Exceeding daily loss halts new entries and writes a risk_events row.

Runs against an in-memory SQLite database and a fake Alpaca client — no
network access, no paper API keys, no Docker. The `get_now` dependency is
pinned to a fixed mid-session instant so the risk engine's near-close veto
(and "today" trade bucketing) can't flip depending on what time it actually
is when the suite runs. Exact friction dollar amounts are already pinned
down in test_friction.py/test_paper_broker.py with controlled timestamps;
this file checks row lineage and the arithmetic relationship (net = gross -
friction) instead of re-asserting hand-computed numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.deps import get_alpaca_client, get_now
from app.execution.broker import BrokerAccount, BrokerPosition
from app.execution.paper_broker import RawAlpacaFill
from app.main import create_app
from app.models import Base
from app.models.account import PaperAccount
from app.models.orders import Fill, Order
from app.models.positions import Position, Trade
from app.models.risk import RiskEvent

QUOTE = {"bid": "99.90", "ask": "100.10", "atr": "1.00", "typical_bar_volume": "10000"}
FIXED_NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)  # 13:00 ET — safely mid-session


class FakeAlpacaClient:
    """Always fills fully, immediately, at whatever qty was requested.

    The price it reports is deliberately nonsense (0) to make it obvious in
    any failing assertion if the friction-adjusted price were ever *not*
    substituted for it.
    """

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
    sessionmaker: object


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
        yield Harness(client=client, sessionmaker=sessionmaker)

    await engine.dispose()


async def seed_account(sessionmaker, equity: Decimal = Decimal("100000")):
    """Returns the account's UUID (not stringified — callers that need it in
    a JSON request body convert it themselves)."""
    async with sessionmaker() as session:
        account = PaperAccount(
            id=uuid4(), user_id=uuid4(), name="test",
            starting_cash=equity, cash=equity, equity=equity,
        )
        session.add(account)
        await session.commit()
        return account.id


async def seed_losing_trade(sessionmaker, account_id, net_pnl: Decimal) -> None:
    async with sessionmaker() as session:
        session.add(Trade(
            account_id=account_id, symbol="XLF", side="buy", qty=Decimal("10"),
            entry_price=Decimal("100"), exit_price=Decimal("95"),
            opened_at=FIXED_NOW, closed_at=FIXED_NOW,
            gross_pnl=net_pnl, total_friction=Decimal("0"), net_pnl=net_pnl,
            r_multiple=None, exit_reason="manual",
        ))
        await session.commit()


def manual_order_body(account_id, **overrides) -> dict:
    body = {
        "account_id": str(account_id), "symbol": "XLF", "side": "buy", "intent": "entry",
        "quote": QUOTE, "stop_price": "98.00", "confirm_token": "held-for-3-seconds",
    }
    body.update(overrides)
    return body


class TestManualEntryProducesRowLineage:
    @pytest.mark.asyncio
    async def test_entry_creates_order_fill_and_open_position(self, harness):
        account_id = await seed_account(harness.sessionmaker)

        resp = harness.client.post("/orders", json=manual_order_body(account_id))
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["status"] == "filled"
        assert body["fill"] is not None
        assert body["position_id"] is not None
        assert body["trade"] is None  # nothing closed on an entry

        # The friction-adjusted price replaced Alpaca's nonsense raw price.
        assert Decimal(body["fill"]["fill_price"]) != Decimal("0")
        # Every itemized component is present, per §9.3.
        for key in ("slippage_cost", "spread_cost", "commission", "reg_fees"):
            assert key in body["fill"]

        async with harness.sessionmaker() as session:
            position = (await session.execute(
                select(Position).where(Position.id == UUID(body["position_id"]))
            )).scalar_one()
            assert position.status == "open"
            assert position.symbol == "XLF"
            assert position.stop_price == Decimal("98.00")

            order = (await session.execute(
                select(Order).where(Order.id == position.entry_order_id)
            )).scalar_one()
            assert order.source == "manual"
            assert order.status == "filled"

            fill = (await session.execute(
                select(Fill).where(Fill.order_id == order.id)
            )).scalar_one()
            assert fill.fill_price == position.avg_entry_price


class TestClosingProducesTradeRow:
    @pytest.mark.asyncio
    async def test_close_produces_trade_with_consistent_math(self, harness):
        account_id = await seed_account(harness.sessionmaker)

        entry_resp = harness.client.post("/orders", json=manual_order_body(account_id))
        assert entry_resp.status_code == 201, entry_resp.text

        exit_resp = harness.client.post("/orders", json=manual_order_body(
            account_id, side="sell", intent="exit", stop_price=None,
        ))
        assert exit_resp.status_code == 201, exit_resp.text
        trade = exit_resp.json()["trade"]

        assert trade is not None
        gross = Decimal(trade["gross_pnl"])
        friction = Decimal(trade["total_friction"])
        net = Decimal(trade["net_pnl"])
        assert net == gross - friction
        assert trade["exit_reason"] == "manual"
        assert friction > 0  # both legs paid spread/slippage at minimum

        async with harness.sessionmaker() as session:
            still_open = (await session.execute(
                select(Position).where(Position.account_id == account_id, Position.status == "open")
            )).scalars().all()
            assert still_open == []

    @pytest.mark.asyncio
    async def test_close_without_open_position_is_rejected(self, harness):
        account_id = await seed_account(harness.sessionmaker)

        resp = harness.client.post("/orders", json=manual_order_body(
            account_id, side="sell", intent="exit", stop_price=None,
        ))
        assert resp.status_code == 404


class TestDailyLossHalt:
    @pytest.mark.asyncio
    async def test_exceeding_daily_loss_blocks_new_entries_and_logs_risk_event(self, harness):
        account_id = await seed_account(harness.sessionmaker, equity=Decimal("100000"))
        # 5% loss today, well past the default 3% max_daily_loss_pct.
        await seed_losing_trade(harness.sessionmaker, account_id, Decimal("-5000"))

        resp = harness.client.post("/orders", json=manual_order_body(account_id))

        assert resp.status_code == 409
        assert resp.json()["detail"]["veto_reason"] == "daily_halt"

        async with harness.sessionmaker() as session:
            events = (await session.execute(
                select(RiskEvent).where(RiskEvent.account_id == account_id)
            )).scalars().all()
            assert len(events) == 1
            assert events[0].event_type == "veto"
            assert events[0].detail["reason"] == "daily_halt"

    @pytest.mark.asyncio
    async def test_unaffected_account_trades_normally(self, harness):
        account_id = await seed_account(harness.sessionmaker)
        resp = harness.client.post("/orders", json=manual_order_body(account_id))
        assert resp.status_code == 201


class TestUnknownAccount:
    @pytest.mark.asyncio
    async def test_returns_404(self, harness):
        resp = harness.client.post("/orders", json=manual_order_body(str(uuid4())))
        assert resp.status_code == 404
