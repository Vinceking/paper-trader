"""Historical bar data fetch + local cache. BUILD_SPEC §8.5/§8.6, Phase 4.

Fetches real historical bars for backtesting from Alpaca's REST *historical
market data* API via alpaca-py's `StockHistoricalDataClient`. This is a data
read, not a trading call — no order is ever submitted here (CLAUDE.md rule
1) — so the same paper-safe credentials used for live paper trading
(`Settings.alpaca_api_key`/`alpaca_api_secret`) are equally safe to use for
this. The IEX feed (`Settings.alpaca_data_feed`) is used throughout to match
what live paper trading actually sees (BUILD_SPEC §8.6/§9.1: IEX is a
partial tape, not the SIP consolidated feed).

Bars are cached to a local JSON file under `.backtest_cache/` (gitignored),
keyed by (symbol, timeframe, start, end), so repeated backtest/test runs
don't re-hit the API. This module intentionally returns plain
`app.ingest.bars.FinalBar` objects — the exact same type the live ingest
pipeline hands to `SymbolEngine` — so the backtest runner in
`app.backtest.runner` can feed historical bars through the identical
bar-finalization/indicator code path as live trading, with no separate
"backtest bar" type to keep in sync.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from app.config import Settings, get_settings
from app.ingest.bars import FinalBar

# backend/.backtest_cache — see backend/.gitignore.
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".backtest_cache"

# The three timeframes the four Phase 3 strategies actually trade on
# (orb/vwap_reversion: 1Min, ema_cross: 5Min, rsi2: 1Day) — deliberately not
# the full app.ingest.bars.TIMEFRAME_SECONDS key set (no "1Hour" strategy
# exists yet).
_TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1Min": TimeFrame.Minute,
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "1Day": TimeFrame.Day,
}


def _cache_path(symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
    key = f"{symbol}|{timeframe}|{start.isoformat()}|{end.isoformat()}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{symbol}_{timeframe}_{digest}.json"


def _bar_to_json(bar: FinalBar) -> dict:
    return {
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "ts": bar.ts.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "vwap": str(bar.vwap) if bar.vwap is not None else None,
        "trade_count": bar.trade_count,
        "source": bar.source,
    }


def _bar_from_json(d: dict) -> FinalBar:
    return FinalBar(
        symbol=d["symbol"],
        timeframe=d["timeframe"],
        ts=datetime.fromisoformat(d["ts"]),
        open=Decimal(d["open"]),
        high=Decimal(d["high"]),
        low=Decimal(d["low"]),
        close=Decimal(d["close"]),
        volume=int(d["volume"]),
        vwap=Decimal(d["vwap"]) if d["vwap"] is not None else None,
        trade_count=int(d["trade_count"]),
        source=d["source"],
    )


def fetch_historical_bars(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> list[FinalBar]:
    """Fetch finalized historical bars for `symbol`/`timeframe` in [start, end).

    `timeframe` is one of "1Min", "5Min", "1Day". Results are sorted oldest
    first (the order `SymbolEngine.on_finalized_bar` requires) and cached to
    disk on first fetch; subsequent calls with the same parameters read the
    cache instead of re-hitting Alpaca.
    """
    if timeframe not in _TIMEFRAME_MAP:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; expected one of {sorted(_TIMEFRAME_MAP)}"
        )

    cache_file = _cache_path(symbol, timeframe, start, end)
    if use_cache and cache_file.exists():
        raw = json.loads(cache_file.read_text())
        return [_bar_from_json(d) for d in raw]

    settings = settings or get_settings()
    client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=_TIMEFRAME_MAP[timeframe],
        start=start,
        end=end,
        feed=settings.alpaca_data_feed,
    )
    bar_set = client.get_stock_bars(request)
    rows = bar_set.data.get(symbol, [])

    bars: list[FinalBar] = []
    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        bars.append(
            FinalBar(
                symbol=symbol,
                timeframe=timeframe,
                ts=ts,
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=int(row.volume),
                vwap=Decimal(str(row.vwap)) if row.vwap is not None else None,
                trade_count=int(row.trade_count) if row.trade_count is not None else 0,
                source=f"alpaca_{settings.alpaca_data_feed}",
            )
        )
    bars.sort(key=lambda b: b.ts)

    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps([_bar_to_json(b) for b in bars]))

    return bars
