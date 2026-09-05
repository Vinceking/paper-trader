"""Strategy CRUD + the backtest gate. BUILD_SPEC §14, §8.5, Phase 4.

CLAUDE.md rule 5, enforced here (not just in the UI): `PATCH /strategies/{id}`
refuses to set `enabled=true` unless the strategy's `gate_passed` is already
`true`, returning 409 with the last gate report's per-criterion detail. There
is no way to enable a strategy from this route without a passing gate.

`POST /strategies/{id}/backtest` runs the full pipeline (fetch -> walk-forward
split -> VectorBT sweep -> event-driven out-of-sample re-verification ->
gate) synchronously, per the task brief's documented simplification (a real
async job queue is out of scope tonight -- BUILD_SPEC §14 calls this "async
job" but Phase 4 only builds the gate itself). For high-frequency strategies
(orb, vwap_reversion at 1Min) over a real multi-year window this is
measured in *minutes*, not milliseconds -- see the Phase 4 task summary. The
historical-data fetch is behind a `HistoricalDataProvider` dependency
(mirrors `app.deps.get_alpaca_client`'s pattern) precisely so tests can
override it with small synthetic bars instead of hitting Alpaca / running
the real event-driven runner over a real 12-month window.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.backtest.data import fetch_historical_bars
from app.backtest.pipeline import run_full_backtest
from app.deps import Clock, CurrentUser, DbSession
from app.ingest.bars import FinalBar
from app.models.gate_reports import GateReportRecord
from app.models.strategies import StrategyRecord
from app.schemas.strategies import (
    BacktestRunOut,
    GateCriterionOut,
    GateReportOut,
    StrategyIn,
    StrategyOut,
    StrategyPatchIn,
)
from app.strategies.registry import STRATEGIES

router = APIRouter(prefix="/strategies", tags=["strategies"])

# Symbol backtested against by default. StrategyRecord (BUILD_SPEC §5) isn't
# scoped to a symbol -- a strategy is a rule set applied across whatever
# watchlist it trades -- so tonight's backtest pipeline runs it against one
# representative liquid symbol from BUILD_SPEC §8.6's universe. A real
# multi-symbol aggregate backtest is future scope, flagged here rather than
# silently assumed.
DEFAULT_BACKTEST_SYMBOL = "SPY"

# How far back to fetch, per timeframe -- chosen to comfortably cover the
# pipeline's in-sample + 12-month out-of-sample split (see
# app.backtest.pipeline._OOS_BAR_COUNT) for whichever timeframe the strategy
# being backtested actually trades on.
_FETCH_LOOKBACK: dict[str, timedelta] = {
    "1Day": timedelta(days=365 * 10),
    "5Min": timedelta(days=365 * 6),
    "1Min": timedelta(days=365 * 6),
}


class HistoricalDataProvider(Protocol):
    def get_bars(
        self, symbol: str, timeframe: str, start, end,
    ) -> list[FinalBar]: ...


class AlpacaHistoricalDataProvider:
    """Default provider: the real Alpaca historical data fetch (app.backtest.data)."""

    def get_bars(self, symbol: str, timeframe: str, start, end) -> list[FinalBar]:
        return fetch_historical_bars(symbol, timeframe, start, end)


@lru_cache
def get_historical_data_provider() -> HistoricalDataProvider:
    return AlpacaHistoricalDataProvider()


HistoricalData = Annotated[HistoricalDataProvider, Depends(get_historical_data_provider)]


def _strategy_out(row: StrategyRecord) -> StrategyOut:
    return StrategyOut(
        id=row.id, slug=row.slug, name=row.name, params=row.params,
        enabled=row.enabled, gate_passed=row.gate_passed,
        gate_report_id=row.gate_report_id, created_at=row.created_at,
    )


def _gate_report_out(row: GateReportRecord) -> GateReportOut:
    return GateReportOut(
        id=row.id, strategy_id=row.strategy_id, gate_passed=row.gate_passed,
        criteria=[GateCriterionOut(**c) for c in row.criteria], created_at=row.created_at,
    )


async def _get_owned_strategy(
    db: DbSession, user: CurrentUser, strategy_id: UUID,
) -> StrategyRecord:
    row = await db.get(StrategyRecord, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="strategy not found")
    return row


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(body: StrategyIn, user: CurrentUser, db: DbSession) -> StrategyOut:
    if body.slug not in STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown strategy slug {body.slug!r}; known slugs: {sorted(STRATEGIES)}",
        )
    row = StrategyRecord(user_id=user.id, slug=body.slug, name=body.name, params=body.params)
    db.add(row)
    await db.commit()
    return _strategy_out(row)


@router.get("", response_model=list[StrategyOut])
async def list_strategies(user: CurrentUser, db: DbSession) -> list[StrategyOut]:
    rows = (
        await db.execute(select(StrategyRecord).where(StrategyRecord.user_id == user.id))
    ).scalars().all()
    return [_strategy_out(r) for r in rows]


@router.patch("/{strategy_id}", response_model=StrategyOut)
async def patch_strategy(
    strategy_id: UUID, body: StrategyPatchIn, user: CurrentUser, db: DbSession,
) -> StrategyOut:
    row = await _get_owned_strategy(db, user, strategy_id)

    if body.enabled and not row.gate_passed:
        detail: dict = {"reason": "gate_not_passed"}
        if row.gate_report_id is not None:
            report = await db.get(GateReportRecord, row.gate_report_id)
            if report is not None:
                detail["gate_report"] = [
                    {"name": c["name"], "passed": c["passed"], "detail": c.get("detail", "")}
                    for c in report.criteria
                ]
        raise HTTPException(status_code=409, detail=detail)

    row.enabled = body.enabled
    await db.commit()
    return _strategy_out(row)


@router.post("/{strategy_id}/backtest", response_model=BacktestRunOut)
async def backtest_strategy(
    strategy_id: UUID,
    user: CurrentUser,
    db: DbSession,
    data: HistoricalData,
    now: Clock,
    symbol: str = Query(default=DEFAULT_BACKTEST_SYMBOL),
) -> BacktestRunOut:
    row = await _get_owned_strategy(db, user, strategy_id)
    timeframe = STRATEGIES[row.slug].timeframe

    lookback = _FETCH_LOOKBACK.get(timeframe, timedelta(days=365 * 6))
    start = now - lookback

    primary_bars = data.get_bars(symbol, timeframe, start, now)
    daily_bars = None
    if timeframe != "1Day":
        daily_bars = data.get_bars(symbol, "1Day", start, now)

    if timeframe == "1Day" and symbol == "SPY":
        spy_daily_bars = primary_bars
    elif symbol == "SPY":
        spy_daily_bars = daily_bars
    else:
        spy_daily_bars = data.get_bars("SPY", "1Day", start, now)

    result = run_full_backtest(
        slug=row.slug,
        params=row.params,
        timeframe=timeframe,
        symbol=symbol,
        primary_bars=primary_bars,
        spy_daily_bars=spy_daily_bars,
        daily_bars=daily_bars,
    )

    report_row = GateReportRecord(
        strategy_id=row.id,
        gate_passed=result.gate_report.gate_passed,
        criteria=[asdict(c) for c in result.gate_report.criteria],
    )
    db.add(report_row)
    await db.flush()  # assigns report_row.id before it's referenced below

    row.gate_passed = result.gate_report.gate_passed
    row.gate_report_id = report_row.id
    await db.commit()

    return BacktestRunOut(
        gate_report=_gate_report_out(report_row),
        winning_params=result.winning_params,
        in_sample_bar_count=result.in_sample_bar_count,
        out_of_sample_bar_count=result.out_of_sample_bar_count,
        out_of_sample_trade_count=result.out_of_sample_trade_count,
    )


@router.get("/{strategy_id}/gate", response_model=GateReportOut)
async def get_gate_report(strategy_id: UUID, user: CurrentUser, db: DbSession) -> GateReportOut:
    row = await _get_owned_strategy(db, user, strategy_id)
    if row.gate_report_id is None:
        raise HTTPException(
            status_code=404, detail="no backtest has been run for this strategy yet"
        )
    report = await db.get(GateReportRecord, row.gate_report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="gate report not found")
    return _gate_report_out(report)
