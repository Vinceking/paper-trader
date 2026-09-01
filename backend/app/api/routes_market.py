"""Market data routes. BUILD_SPEC §14."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.ingest.bars import TIMEFRAME_SECONDS

router = APIRouter(prefix="/market", tags=["market"])


class BarOut(BaseModel):
    symbol: str
    timeframe: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None


class WatchlistOut(BaseModel):
    symbols: list[str]
    max_symbols: int
    remaining: int


@router.get("/watchlist", response_model=WatchlistOut)
async def get_watchlist() -> WatchlistOut:
    s = get_settings()
    symbols = list(s.default_watchlist)
    return WatchlistOut(
        symbols=symbols,
        max_symbols=s.max_stream_symbols,
        remaining=max(0, s.max_stream_symbols - len(symbols)),
    )


@router.get("/bars", response_model=list[BarOut])
async def get_bars(
    symbol: str = Query(..., min_length=1, max_length=16),
    timeframe: str = Query("1Min"),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(500, ge=1, le=10_000),
) -> list[BarOut]:
    if timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown timeframe {timeframe!r}; "
                   f"expected one of {sorted(TIMEFRAME_SECONDS)}",
        )
    # Phase 1: returns [] until the DB session dependency is wired.
    return []
