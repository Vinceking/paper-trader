"""`/strategies*` route integration tests. BUILD_SPEC §14, §8.5, Phase 4
acceptance criteria:

✅ Attempting to enable a strategy with `gate_passed = false` returns 409.
✅ Gate report shows pass/fail per criterion with the actual value.

Runs against an in-memory SQLite database (same pattern as
tests/test_orders_api.py / tests/test_auth.py) with a real registered user
(so `CurrentUser` resolves normally) and a FAKE `HistoricalDataProvider`
override so `POST /strategies/{id}/backtest` runs the real pipeline
(sweep -> verify -> gate) end to end but against small synthetic bars
instead of hitting Alpaca or running the event-driven runner over a real
multi-year window (which takes minutes, not milliseconds -- see
app/backtest/pipeline.py's docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.routes_strategies import get_historical_data_provider
from app.db import get_session
from app.deps import get_now
from app.ingest.bars import FinalBar
from app.main import create_app
from app.models import Base
from app.models.gate_reports import GateReportRecord
from app.models.strategies import StrategyRecord

FIXED_NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)


def _daily_bars(n: int, symbol: str = "SPY") -> list[FinalBar]:
    start = FIXED_NOW - timedelta(days=n)
    bars = []
    for i in range(n):
        close = Decimal(100) + Decimal(i) * Decimal("0.05")
        bars.append(
            FinalBar(
                symbol=symbol, timeframe="1Day", ts=start + timedelta(days=i),
                open=close - Decimal("0.2"), high=close + Decimal("0.5"),
                low=close - Decimal("0.5"), close=close,
                volume=1_000_000, vwap=None, trade_count=1000,
            )
        )
    return bars


class FakeDataProvider:
    """Deterministic, small, fast -- see module docstring."""

    def get_bars(self, symbol: str, timeframe: str, start, end) -> list[FinalBar]:
        if timeframe == "1Day":
            return _daily_bars(300, symbol=symbol)
        return []  # no 1Min/5Min data in this fake -- not needed for rsi2


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
    app.dependency_overrides[get_now] = lambda: FIXED_NOW
    app.dependency_overrides[get_historical_data_provider] = lambda: FakeDataProvider()

    with TestClient(app) as client:
        yield Harness(client=client, sessionmaker=sessionmaker)

    await engine.dispose()


def _auth_headers(client: TestClient, email: str = "trader@example.com") -> dict:
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": "hunter2pass", "display_name": "Trader"},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestCreateAndList:
    def test_create_rejects_unknown_slug(self, harness):
        headers = _auth_headers(harness.client)
        resp = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "not_a_real_strategy", "name": "x", "params": {}},
        )
        assert resp.status_code == 400

    def test_create_and_list_round_trip(self, harness):
        headers = _auth_headers(harness.client)
        resp = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["slug"] == "rsi2"
        assert body["enabled"] is False
        assert body["gate_passed"] is False

        listed = harness.client.get("/strategies", headers=headers).json()
        assert len(listed) == 1
        assert listed[0]["id"] == body["id"]

    def test_strategies_are_scoped_per_user(self, harness):
        headers_a = _auth_headers(harness.client, "a@example.com")
        headers_b = _auth_headers(harness.client, "b@example.com")
        harness.client.post(
            "/strategies", headers=headers_a,
            json={"slug": "rsi2", "name": "Alice's", "params": {}},
        )
        assert harness.client.get("/strategies", headers=headers_b).json() == []


class TestPatchEnableGate:
    def test_enabling_with_gate_not_passed_returns_409(self, harness):
        headers = _auth_headers(harness.client)
        created = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        ).json()

        resp = harness.client.patch(
            f"/strategies/{created['id']}", headers=headers, json={"enabled": True},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "gate_not_passed"

    def test_disabling_never_requires_a_passed_gate(self, harness):
        headers = _auth_headers(harness.client)
        created = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        ).json()
        resp = harness.client.patch(
            f"/strategies/{created['id']}", headers=headers, json={"enabled": False},
        )
        assert resp.status_code == 200

    def test_enabling_succeeds_once_gate_passed_is_true(self, harness):
        headers = _auth_headers(harness.client)
        created = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        ).json()

        async def force_gate_passed():
            async with harness.sessionmaker() as session:
                row = await session.get(StrategyRecord, UUID(created["id"]))
                row.gate_passed = True
                await session.commit()

        import asyncio
        asyncio.run(force_gate_passed())

        resp = harness.client.patch(
            f"/strategies/{created['id']}", headers=headers, json={"enabled": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is True

    def test_unknown_strategy_id_returns_404(self, harness):
        headers = _auth_headers(harness.client)
        resp = harness.client.patch(
            "/strategies/00000000-0000-0000-0000-000000000000",
            headers=headers, json={"enabled": True},
        )
        assert resp.status_code == 404


class TestBacktestAndGateEndpoints:
    def test_backtest_runs_and_persists_a_gate_report(self, harness):
        headers = _auth_headers(harness.client)
        created = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        ).json()

        resp = harness.client.post(
            f"/strategies/{created['id']}/backtest", headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        report = body["gate_report"]
        criterion_names = {c["name"] for c in report["criteria"]}
        assert criterion_names == {
            "sample_size", "oos_period_months", "expectancy_after_friction",
            "profit_factor", "max_drawdown_pct", "walk_forward_efficiency",
            "beats_spy_buy_and_hold",
        }
        # Every criterion carries pass/fail AND the actual value (Phase 4
        # acceptance criterion, verbatim).
        for c in report["criteria"]:
            assert isinstance(c["passed"], bool)
            assert isinstance(c["actual"], (int, float))

        # The strategy row reflects whatever the gate actually decided --
        # not asserted as True/False here since it's a genuine, un-rigged
        # outcome on tiny synthetic data (see CLAUDE.md's anti-fabrication
        # rule -- this test does not force a particular result).
        strategy_after = harness.client.get("/strategies", headers=headers).json()[0]
        assert strategy_after["gate_passed"] == report["gate_passed"]
        assert strategy_after["gate_report_id"] == report["id"]

    def test_gate_endpoint_returns_the_persisted_report(self, harness):
        headers = _auth_headers(harness.client)
        created = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        ).json()
        backtest_resp = harness.client.post(
            f"/strategies/{created['id']}/backtest", headers=headers,
        ).json()

        gate_resp = harness.client.get(
            f"/strategies/{created['id']}/gate", headers=headers,
        )
        assert gate_resp.status_code == 200
        gate_body = gate_resp.json()
        assert gate_body["id"] == backtest_resp["gate_report"]["id"]
        assert gate_body["gate_passed"] == backtest_resp["gate_report"]["gate_passed"]
        assert len(gate_body["criteria"]) == 7

    def test_gate_endpoint_404s_before_any_backtest_has_run(self, harness):
        headers = _auth_headers(harness.client)
        created = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        ).json()
        resp = harness.client.get(f"/strategies/{created['id']}/gate", headers=headers)
        assert resp.status_code == 404

    def test_backtest_creates_a_gate_reports_row(self, harness):
        headers = _auth_headers(harness.client)
        created = harness.client.post(
            "/strategies", headers=headers,
            json={"slug": "rsi2", "name": "My RSI2", "params": {}},
        ).json()
        harness.client.post(f"/strategies/{created['id']}/backtest", headers=headers)

        async def count_reports():
            async with harness.sessionmaker() as session:
                from sqlalchemy import select
                rows = (
                    await session.execute(
                        select(GateReportRecord).where(
                            GateReportRecord.strategy_id == UUID(created["id"])
                        )
                    )
                ).scalars().all()
                return len(rows)

        import asyncio
        assert asyncio.run(count_reports()) == 1
