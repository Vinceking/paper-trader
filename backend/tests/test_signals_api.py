"""`GET /signals` integration tests. BUILD_SPEC §14: "includes vetoed signals".

Runs against an in-memory SQLite database with a real registered user (same
pattern as tests/test_strategies_api.py / tests/test_orders_api.py).
`SignalRecord` rows are seeded directly via the session -- there is no
create-signal endpoint; that's app/ingest/pipeline.py's job, covered
separately in tests/test_ingest_pipeline.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import create_app
from app.models import Base
from app.models.account import PaperAccount
from app.models.signals import SignalRecord
from app.models.strategies import StrategyRecord

NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)


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


def _auth_headers(client: TestClient, email: str = "trader@example.com") -> tuple[dict, UUID]:
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": "hunter2pass", "display_name": "Trader"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, UUID(body["user_id"])


async def _seed_strategy_and_signals(
    sessionmaker, user_id: UUID, n: int, acted_on_pattern: list[bool] | None = None,
) -> list[UUID]:
    """Seeds one strategy (owned by `user_id`) and `n` signals against the
    caller's own PaperAccount, oldest ts first in the args but returned in
    creation order. `acted_on_pattern` overrides acted_on per index."""
    async with sessionmaker() as session:
        account = (
            await session.execute(select(PaperAccount).where(PaperAccount.user_id == user_id))
        ).scalar_one()

        strategy = StrategyRecord(
            user_id=user_id, slug="rsi2", name="My RSI2", params={},
        )
        session.add(strategy)
        await session.flush()

        ids: list[UUID] = []
        for i in range(n):
            acted_on = acted_on_pattern[i] if acted_on_pattern else False
            record = SignalRecord(
                strategy_id=strategy.id, account_id=account.id, symbol="XLF",
                ts=NOW + timedelta(minutes=i), side="buy", intent="entry",
                rule_id="rsi2.oversold_long", rule_text="RSI(2) < 10 and price > SMA200",
                features={"rsi_2": 7.3 + i},
                conditions=[
                    {
                        "name": "rsi_below_threshold", "description": "RSI(2) below oversold",
                        "operator": "<", "threshold": 10.0, "actual": 7.3, "passed": True,
                    },
                    {
                        "name": "above_sma200", "description": "price above 200-SMA",
                        "operator": ">", "threshold": 48.0, "actual": 47.9, "passed": False,
                    },
                ],
                confidence=Decimal("0.62"), acted_on=acted_on,
            )
            session.add(record)
            await session.flush()
            ids.append(record.id)
        await session.commit()
        return ids


class TestScoping:
    def test_a_user_never_sees_another_users_signals(self, harness):
        headers_a, user_a = _auth_headers(harness.client, "a@example.com")
        headers_b, user_b = _auth_headers(harness.client, "b@example.com")

        asyncio.run(_seed_strategy_and_signals(harness.sessionmaker, user_a, 3))

        resp_a = harness.client.get("/signals", headers=headers_a)
        assert resp_a.status_code == 200
        assert len(resp_a.json()) == 3

        resp_b = harness.client.get("/signals", headers=headers_b)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    def test_requires_auth(self, harness):
        resp = harness.client.get("/signals")
        assert resp.status_code == 401


class TestResponseShape:
    def test_includes_strategy_slug_and_name_and_full_evidence(self, harness):
        headers, user_id = _auth_headers(harness.client)
        asyncio.run(_seed_strategy_and_signals(harness.sessionmaker, user_id, 1))

        resp = harness.client.get("/signals", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        row = body[0]

        assert row["symbol"] == "XLF"
        assert row["side"] == "buy"
        assert row["intent"] == "entry"
        assert row["rule_id"] == "rsi2.oversold_long"
        assert row["rule_text"]
        assert row["strategy_slug"] == "rsi2"
        assert row["strategy_name"] == "My RSI2"
        assert row["acted_on"] is False
        assert row["veto_reason"] is None
        # Full evidence, including the failed condition -- CLAUDE.md rule 2.
        assert len(row["conditions"]) == 2
        passed_flags = {c["name"]: c["passed"] for c in row["conditions"]}
        assert passed_flags["rsi_below_threshold"] is True
        assert passed_flags["above_sma200"] is False
        assert row["features"]["rsi_2"] == 7.3

    def test_ordered_most_recent_first(self, harness):
        headers, user_id = _auth_headers(harness.client)
        asyncio.run(_seed_strategy_and_signals(harness.sessionmaker, user_id, 3))

        resp = harness.client.get("/signals", headers=headers)
        timestamps = [row["ts"] for row in resp.json()]
        assert timestamps == sorted(timestamps, reverse=True)


class TestActedOnFilter:
    def test_filters_by_acted_on_true(self, harness):
        headers, user_id = _auth_headers(harness.client)
        asyncio.run(
            _seed_strategy_and_signals(
                harness.sessionmaker, user_id, 4,
                acted_on_pattern=[True, False, True, False],
            )
        )

        resp = harness.client.get("/signals", headers=headers, params={"acted_on": "true"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert all(row["acted_on"] is True for row in body)

    def test_filters_by_acted_on_false(self, harness):
        headers, user_id = _auth_headers(harness.client)
        asyncio.run(
            _seed_strategy_and_signals(
                harness.sessionmaker, user_id, 4,
                acted_on_pattern=[True, False, True, False],
            )
        )

        resp = harness.client.get("/signals", headers=headers, params={"acted_on": "false"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert all(row["acted_on"] is False for row in body)

    def test_no_filter_returns_everything(self, harness):
        headers, user_id = _auth_headers(harness.client)
        asyncio.run(
            _seed_strategy_and_signals(
                harness.sessionmaker, user_id, 4,
                acted_on_pattern=[True, False, True, False],
            )
        )

        resp = harness.client.get("/signals", headers=headers)
        assert len(resp.json()) == 4


class TestLimitBounds:
    def test_default_limit_is_50(self, harness):
        headers, user_id = _auth_headers(harness.client)
        asyncio.run(_seed_strategy_and_signals(harness.sessionmaker, user_id, 60))

        resp = harness.client.get("/signals", headers=headers)
        assert len(resp.json()) == 50

    def test_limit_is_respected(self, harness):
        headers, user_id = _auth_headers(harness.client)
        asyncio.run(_seed_strategy_and_signals(harness.sessionmaker, user_id, 10))

        resp = harness.client.get("/signals", headers=headers, params={"limit": 3})
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_limit_above_max_is_rejected(self, harness):
        headers, _ = _auth_headers(harness.client)
        resp = harness.client.get("/signals", headers=headers, params={"limit": 201})
        assert resp.status_code == 422

    def test_limit_below_one_is_rejected(self, harness):
        headers, _ = _auth_headers(harness.client)
        resp = harness.client.get("/signals", headers=headers, params={"limit": 0})
        assert resp.status_code == 422
