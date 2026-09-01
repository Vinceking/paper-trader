"""API smoke tests. Phase 1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.routes_health import build_health
from app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["live_trading_enabled"] is False

    def test_health_flags_partial_tape_on_iex(self, client):
        """The IEX limitation must be surfaced, never hidden. BUILD_SPEC §2.3."""
        assert client.get("/health").json()["partial_tape"] is True

    def test_stale_symbol_degrades_status(self):
        now = datetime.now(timezone.utc)
        resp = build_health(
            ingest_rows=[("SPY", now - timedelta(minutes=10))],
            unresolved_gaps=0, now=now,
        )
        assert resp.status == "degraded"
        assert resp.symbols[0].stale is True

    def test_fresh_symbol_is_ok(self):
        now = datetime.now(timezone.utc)
        resp = build_health(
            ingest_rows=[("SPY", now - timedelta(seconds=30))],
            unresolved_gaps=0, now=now,
        )
        assert resp.status == "ok"
        assert resp.symbols[0].stale is False

    def test_unresolved_gap_degrades_status(self):
        resp = build_health(ingest_rows=[], unresolved_gaps=1)
        assert resp.status == "degraded"


class TestMarket:
    def test_watchlist_reports_free_tier_cap(self, client):
        body = client.get("/market/watchlist").json()
        assert body["max_symbols"] == 30
        assert body["remaining"] == 30 - len(body["symbols"])

    def test_bars_rejects_unknown_timeframe(self, client):
        r = client.get("/market/bars", params={"symbol": "XLF", "timeframe": "3Min"})
        assert r.status_code == 422

    def test_bars_accepts_known_timeframe(self, client):
        r = client.get("/market/bars", params={"symbol": "XLF", "timeframe": "5Min"})
        assert r.status_code == 200

    def test_bars_requires_symbol(self, client):
        assert client.get("/market/bars").status_code == 422
