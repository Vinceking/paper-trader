"""Strategy/gate API schemas. BUILD_SPEC §14, §8.5, Phase 4."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class StrategyIn(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)


class StrategyPatchIn(BaseModel):
    enabled: bool


class StrategyOut(BaseModel):
    id: UUID
    slug: str
    name: str
    params: dict
    enabled: bool
    gate_passed: bool
    gate_report_id: UUID | None
    created_at: datetime


class GateCriterionOut(BaseModel):
    name: str
    threshold: float
    actual: float
    passed: bool
    detail: str


class GateReportOut(BaseModel):
    id: UUID
    strategy_id: UUID
    gate_passed: bool
    criteria: list[GateCriterionOut]
    created_at: datetime


class BacktestRunOut(BaseModel):
    gate_report: GateReportOut
    winning_params: dict
    in_sample_bar_count: int
    out_of_sample_bar_count: int
    out_of_sample_trade_count: int
