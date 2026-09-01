"""Health endpoint.

Phase 1 acceptance criterion: /health reports last_bar_ts per symbol. A silent
gap in the bar series corrupts every indicator downstream, so staleness has to be
visible without anyone going to look for it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(tags=["health"])

STALE_AFTER_SECONDS = 120


class SymbolHealth(BaseModel):
    symbol: str
    last_bar_ts: datetime | None
    seconds_since_last_bar: float | None
    stale: bool


class HealthResponse(BaseModel):
    status: str
    environment: str
    data_feed: str
    partial_tape: bool
    live_trading_enabled: bool
    unresolved_gaps: int
    symbols: list[SymbolHealth]


def build_health(
    ingest_rows: list[tuple[str, datetime | None]],
    unresolved_gaps: int,
    now: datetime | None = None,
) -> HealthResponse:
    """Pure builder so this is unit-testable without a database."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)

    symbols: list[SymbolHealth] = []
    any_stale = False
    for symbol, last_ts in ingest_rows:
        if last_ts is None:
            symbols.append(SymbolHealth(
                symbol=symbol, last_bar_ts=None,
                seconds_since_last_bar=None, stale=False,
            ))
            continue
        age = (now - last_ts).total_seconds()
        stale = age > STALE_AFTER_SECONDS
        any_stale = any_stale or stale
        symbols.append(SymbolHealth(
            symbol=symbol, last_bar_ts=last_ts,
            seconds_since_last_bar=round(age, 1), stale=stale,
        ))

    status = "degraded" if (any_stale or unresolved_gaps) else "ok"
    return HealthResponse(
        status=status,
        environment=settings.environment,
        data_feed=settings.alpaca_data_feed,
        partial_tape=settings.alpaca_data_feed == "iex",
        live_trading_enabled=settings.enable_live_trading,
        unresolved_gaps=unresolved_gaps,
        symbols=symbols,
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # Phase 1: wired to the ingest_state table once the DB session dependency
    # lands. Until then this reports process liveness with an empty symbol set.
    return build_health(ingest_rows=[], unresolved_gaps=0)
