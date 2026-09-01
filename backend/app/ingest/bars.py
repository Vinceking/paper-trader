"""Bar construction, finalization, and gap detection.

BUILD_SPEC §7.2. Two rules matter here and both are load-bearing:

1. Bar timestamps are BAR-OPEN time, in UTC.
2. A bar is not final until the next bar's first tick arrives, or the minute
   boundary plus a grace period has elapsed. Strategies evaluate ONLY finalized
   bars — evaluating a forming bar is a lookahead bug that makes backtests look
   brilliant and live results terrible.

Everything in this module is pure and synchronous so it can be property-tested
without a database or a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

TIMEFRAME_SECONDS: dict[str, int] = {
    "1Min": 60,
    "5Min": 300,
    "15Min": 900,
    "1Hour": 3600,
}


def floor_to_timeframe(ts: datetime, timeframe: str) -> datetime:
    """Return the bar-open timestamp containing `ts`."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    ts = ts.astimezone(timezone.utc)
    seconds = TIMEFRAME_SECONDS[timeframe]
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


@dataclass
class WorkingBar:
    """A bar still being built. Not written to the database until finalized."""

    symbol: str
    timeframe: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    trade_count: int = 0
    _notional: Decimal = field(default=Decimal("0"), repr=False)

    @property
    def vwap(self) -> Decimal | None:
        if self.volume == 0:
            return None
        return (self._notional / Decimal(self.volume)).quantize(Decimal("0.0001"))

    def add_trade(self, price: Decimal, size: int) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size
        self.trade_count += 1
        self._notional += price * Decimal(size)

    def end_ts(self) -> datetime:
        return self.ts + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])


@dataclass(frozen=True)
class FinalBar:
    symbol: str
    timeframe: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None
    trade_count: int
    source: str = "alpaca_iex"


class BarBuilder:
    """Aggregates trades into bars for a single symbol/timeframe.

    Usage:
        builder = BarBuilder("SPY", "1Min", grace_seconds=2.0)
        final = builder.on_trade(ts, price, size)   # -> FinalBar | None
        final = builder.maybe_finalize(now)         # -> FinalBar | None
    """

    def __init__(self, symbol: str, timeframe: str = "1Min", grace_seconds: float = 2.0):
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        self.symbol = symbol
        self.timeframe = timeframe
        self.grace = timedelta(seconds=grace_seconds)
        self._working: WorkingBar | None = None

    @property
    def working(self) -> WorkingBar | None:
        return self._working

    def on_trade(self, ts: datetime, price: Decimal, size: int) -> FinalBar | None:
        """Add a trade. Returns the previous bar if this trade closed it."""
        bar_ts = floor_to_timeframe(ts, self.timeframe)
        finalized: FinalBar | None = None

        if self._working is None:
            self._working = self._new_bar(bar_ts, price)
        elif bar_ts > self._working.ts:
            # First tick of the next bar finalizes the current one.
            finalized = self._finalize()
            self._working = self._new_bar(bar_ts, price)
        elif bar_ts < self._working.ts:
            # Late/out-of-order tick for an already-closed bar. Drop it rather
            # than mutating history — a rewritten bar silently invalidates every
            # indicator computed from it.
            return None

        assert self._working is not None
        self._working.add_trade(price, size)
        return finalized

    def maybe_finalize(self, now: datetime) -> FinalBar | None:
        """Finalize on the wall clock when no further ticks have arrived.

        A thin or illiquid symbol may print no trades in the following minute, so
        finalization cannot depend on the next tick alone.
        """
        if self._working is None:
            return None
        if now >= self._working.end_ts() + self.grace:
            return self._finalize()
        return None

    def _new_bar(self, bar_ts: datetime, price: Decimal) -> WorkingBar:
        return WorkingBar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            ts=bar_ts,
            open=price,
            high=price,
            low=price,
            close=price,
        )

    def _finalize(self) -> FinalBar:
        w = self._working
        assert w is not None
        self._working = None
        return FinalBar(
            symbol=w.symbol,
            timeframe=w.timeframe,
            ts=w.ts,
            open=w.open,
            high=w.high,
            low=w.low,
            close=w.close,
            volume=w.volume,
            vwap=w.vwap,
            trade_count=w.trade_count,
        )


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    expected_bars: int


def detect_gaps(
    symbol: str,
    timeframe: str,
    have: list[datetime],
    session_start: datetime,
    session_end: datetime,
) -> list[Gap]:
    """Find missing bar slots between session_start and session_end.

    `have` is the list of bar-open timestamps actually present. Both bounds are
    treated as [start, end): a bar opening exactly at session_end is out of scope.

    This is deliberately dumb and exhaustive. It runs on reconnect and at EOD, so
    correctness matters far more than speed.
    """
    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    present = {floor_to_timeframe(t, timeframe) for t in have}

    expected: list[datetime] = []
    cursor = floor_to_timeframe(session_start, timeframe)
    while cursor < session_end:
        expected.append(cursor)
        cursor += step

    gaps: list[Gap] = []
    run_start: datetime | None = None
    run_len = 0

    for slot in expected:
        if slot in present:
            if run_start is not None:
                gaps.append(Gap(symbol, timeframe, run_start, slot, run_len))
                run_start, run_len = None, 0
        else:
            if run_start is None:
                run_start = slot
            run_len += 1

    if run_start is not None:
        gaps.append(Gap(symbol, timeframe, run_start, expected[-1] + step, run_len))

    return gaps
