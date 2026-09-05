"""Market data routes. BUILD_SPEC §14."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.ingest.bars import TIMEFRAME_SECONDS, FinalBar
from app.strategies.indicators import compute_indicators

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


class ReferenceQuoteOut(BaseModel):
    symbol: str
    bid: float
    ask: float
    atr_14: float | None
    typical_bar_volume: float | None
    ts: datetime


@router.get("/quote/{symbol}", response_model=ReferenceQuoteOut)
async def get_reference_quote(symbol: str) -> ReferenceQuoteOut:
    """A live bid/ask plus enough context (ATR, typical volume) for the
    manual-order screen to fill in POST /orders' `quote` field without the
    person placing the trade needing to know what ATR is.

    Fetched directly from Alpaca's REST historical/quote API rather than
    through the ingest/WebSocket pipeline — that pipeline was never wired
    through to Postgres/Redis (see README's Phase 2 scope notes), and this
    is a much smaller, self-contained way to get a real, live number in
    front of a real user tonight. `atr_14`/`typical_bar_volume` come from
    the last 30 daily bars via the same `compute_indicators` the strategy
    engine uses — not a separate, parallel calculation.
    """
    settings = get_settings()
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)
    symbol = symbol.upper()

    try:
        quotes = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502, not a 500 traceback
        raise HTTPException(status_code=502, detail=f"could not fetch a live quote: {exc}") from exc

    quote = quotes.get(symbol)
    if quote is None or quote.bid_price <= 0 or quote.ask_price <= 0:
        raise HTTPException(status_code=404, detail=f"no live quote available for {symbol!r}")

    atr_14: float | None = None
    typical_bar_volume: float | None = None
    try:
        end = datetime.now(UTC) - timedelta(minutes=20)  # IEX free tier lag
        start = end - timedelta(days=45)
        bar_set = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end,
            )
        )
        rows = list(bar_set.data.get(symbol, []))
        if rows:
            history = [
                FinalBar(
                    symbol=symbol, timeframe="1Day", ts=row.timestamp,
                    open=Decimal(str(row.open)), high=Decimal(str(row.high)),
                    low=Decimal(str(row.low)), close=Decimal(str(row.close)),
                    volume=int(row.volume), vwap=None, trade_count=row.trade_count,
                )
                for row in rows
            ]
            snapshot = compute_indicators(history)
            atr_14 = snapshot.atr_14
            if snapshot.relative_volume_20 and history[-1].volume:
                typical_bar_volume = history[-1].volume / snapshot.relative_volume_20
    except Exception:  # noqa: BLE001 - degrade gracefully, the quote itself still works
        pass

    return ReferenceQuoteOut(
        symbol=symbol,
        bid=float(quote.bid_price),
        ask=float(quote.ask_price),
        atr_14=atr_14,
        typical_bar_volume=typical_bar_volume,
        ts=quote.timestamp,
    )
