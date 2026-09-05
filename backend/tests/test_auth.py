"""Auth + account-read integration tests.

Runs against an in-memory SQLite database via FastAPI dependency overrides —
same pattern as tests/test_orders_api.py. Covers the acceptance criteria for
tonight's family-login addition:

✅ Register creates a User + a PaperAccount with $100,000 starting cash,
   returns a valid token.
✅ Duplicate email registration returns 409.
✅ Login: correct credentials -> valid token; wrong password -> 401;
   unknown email -> 401 (identical error either way).
✅ GET /account: valid token -> that user's account; invalid/missing/expired
   token -> 401.
✅ Two different users each get their OWN PaperAccount — user A can never
   see user B's account/positions/trades.
✅ GET /account/positions and GET /account/trades: empty for a fresh
   account, real rows once inserted directly via the DB session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest_asyncio
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.security import ALGORITHM
from app.config import get_settings
from app.db import get_session
from app.main import create_app
from app.models import Base
from app.models.positions import Position, Trade

FIXED_NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)


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

    with TestClient(app) as client:
        yield Harness(client=client, sessionmaker=sessionmaker)

    await engine.dispose()


def register(client: TestClient, email: str, password: str = "hunter2pass", name: str = "Kid"):
    return client.post(
        "/auth/register",
        json={"email": email, "password": password, "display_name": name},
    )


class TestRegister:
    def test_register_creates_user_and_100k_account_and_returns_token(self, harness):
        resp = register(harness.client, "teen@family.example")
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["token_type"] == "bearer"
        assert body["display_name"] == "Kid"
        assert "user_id" in body
        assert isinstance(body["access_token"], str) and body["access_token"]

        # Token works against a protected route and returns a $100k account.
        me = harness.client.get(
            "/account", headers={"Authorization": f"Bearer {body['access_token']}"}
        )
        assert me.status_code == 200, me.text
        account = me.json()
        assert Decimal(account["cash"]) == Decimal("100000.00")
        assert Decimal(account["equity"]) == Decimal("100000.00")
        assert Decimal(account["starting_cash"]) == Decimal("100000.00")

    def test_duplicate_email_returns_409(self, harness):
        first = register(harness.client, "dad@family.example")
        assert first.status_code == 201

        second = register(harness.client, "dad@family.example")
        assert second.status_code == 409


class TestLogin:
    def test_login_with_correct_credentials_returns_valid_token(self, harness):
        register(harness.client, "mom@family.example", password="correct-horse-1")

        resp = harness.client.post(
            "/auth/login",
            json={"email": "mom@family.example", "password": "correct-horse-1"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]

        me = harness.client.get("/account", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200

    def test_login_wrong_password_returns_401(self, harness):
        register(harness.client, "mom2@family.example", password="correct-horse-1")

        resp = harness.client.post(
            "/auth/login",
            json={"email": "mom2@family.example", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        wrong_password_detail = resp.json()["detail"]

        unknown = harness.client.post(
            "/auth/login",
            json={"email": "nobody@family.example", "password": "whatever12"},
        )
        assert unknown.status_code == 401
        # Same generic error either way — don't leak which part was wrong.
        assert unknown.json()["detail"] == wrong_password_detail

    def test_login_unknown_email_returns_401(self, harness):
        resp = harness.client.post(
            "/auth/login",
            json={"email": "ghost@family.example", "password": "whatever12"},
        )
        assert resp.status_code == 401


class TestAccountAuthGuard:
    def test_missing_token_returns_401(self, harness):
        resp = harness.client.get("/account")
        assert resp.status_code == 401

    def test_garbage_token_returns_401(self, harness):
        resp = harness.client.get(
            "/account", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert resp.status_code == 401

    def test_expired_token_returns_401(self, harness):
        settings = get_settings()
        expired = jwt.encode(
            {
                "sub": str(uuid4()),
                "role": "requester",
                "exp": datetime.now(UTC) - timedelta(minutes=5),
            },
            settings.jwt_secret,
            algorithm=ALGORITHM,
        )
        resp = harness.client.get(
            "/account", headers={"Authorization": f"Bearer {expired}"}
        )
        assert resp.status_code == 401

    def test_token_for_deleted_or_unknown_user_returns_401(self, harness):
        settings = get_settings()
        token = jwt.encode(
            {
                "sub": str(uuid4()),  # a user id that was never registered
                "role": "requester",
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            settings.jwt_secret,
            algorithm=ALGORITHM,
        )
        resp = harness.client.get(
            "/account", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401


class TestPerUserIsolation:
    def test_user_a_cannot_see_user_bs_account_positions_or_trades(self, harness):
        token_a = register(harness.client, "alice@family.example", name="Alice").json()[
            "access_token"
        ]
        token_b = register(harness.client, "bob@family.example", name="Bob").json()[
            "access_token"
        ]
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        account_a = harness.client.get("/account", headers=headers_a).json()
        account_b = harness.client.get("/account", headers=headers_b).json()
        # Separate accounts entirely.
        assert account_a["id"] != account_b["id"]

        async def seed_position_for(account_id: str):
            async with harness.sessionmaker() as session:
                session.add(Position(
                    account_id=UUID(account_id), entry_order_id=uuid4(), symbol="XLF",
                    qty=Decimal("10"), avg_entry_price=Decimal("100"),
                    stop_price=Decimal("98"), opened_at=FIXED_NOW, status="open",
                ))
                await session.commit()

        async def seed_trade_for(account_id: str):
            async with harness.sessionmaker() as session:
                session.add(Trade(
                    account_id=UUID(account_id), symbol="XLF", side="buy", qty=Decimal("10"),
                    entry_price=Decimal("100"), exit_price=Decimal("105"),
                    opened_at=FIXED_NOW, closed_at=FIXED_NOW,
                    gross_pnl=Decimal("50"), total_friction=Decimal("5"),
                    net_pnl=Decimal("45"), r_multiple=Decimal("1.5"), exit_reason="manual",
                ))
                await session.commit()

        # Seed only for Bob's account.
        import asyncio

        asyncio.run(seed_position_for(account_b["id"]))
        asyncio.run(seed_trade_for(account_b["id"]))

        # Alice sees nothing.
        positions_a = harness.client.get("/account/positions", headers=headers_a).json()
        trades_a = harness.client.get("/account/trades", headers=headers_a).json()
        assert positions_a == []
        assert trades_a == []

        # Bob sees his own rows.
        positions_b = harness.client.get("/account/positions", headers=headers_b).json()
        trades_b = harness.client.get("/account/trades", headers=headers_b).json()
        assert len(positions_b) == 1
        assert positions_b[0]["symbol"] == "XLF"
        assert len(trades_b) == 1
        assert trades_b[0]["symbol"] == "XLF"


class TestPositionsAndTradesEndpoints:
    def test_empty_lists_for_fresh_account(self, harness):
        token = register(harness.client, "fresh@family.example").json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert harness.client.get("/account/positions", headers=headers).json() == []
        assert harness.client.get("/account/trades", headers=headers).json() == []

    def test_returns_real_rows_once_inserted(self, harness):
        resp = register(harness.client, "trader@family.example")
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        account_id = harness.client.get("/account", headers=headers).json()["id"]

        import asyncio

        async def seed():
            async with harness.sessionmaker() as session:
                session.add(Position(
                    account_id=UUID(account_id), entry_order_id=uuid4(), symbol="QQQ",
                    qty=Decimal("5"), avg_entry_price=Decimal("400"),
                    stop_price=Decimal("395"), opened_at=FIXED_NOW, status="open",
                ))
                session.add(Trade(
                    account_id=UUID(account_id), symbol="SPY", side="buy", qty=Decimal("3"),
                    entry_price=Decimal("500"), exit_price=Decimal("510"),
                    opened_at=FIXED_NOW, closed_at=FIXED_NOW,
                    gross_pnl=Decimal("30"), total_friction=Decimal("3"),
                    net_pnl=Decimal("27"), r_multiple=Decimal("2.0"), exit_reason="target",
                ))
                await session.commit()

        asyncio.run(seed())

        positions = harness.client.get("/account/positions", headers=headers).json()
        trades = harness.client.get("/account/trades", headers=headers).json()

        assert len(positions) == 1
        assert positions[0]["symbol"] == "QQQ"
        assert positions[0]["status"] == "open"

        assert len(trades) == 1
        assert trades[0]["symbol"] == "SPY"
        assert trades[0]["exit_reason"] == "target"

    def test_trades_limit_query_param_is_capped(self, harness):
        resp = register(harness.client, "capped@family.example")
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        over_cap = harness.client.get("/account/trades?limit=500", headers=headers)
        assert over_cap.status_code == 422  # exceeds max=200
