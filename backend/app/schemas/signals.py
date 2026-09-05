"""Signal read-path API schema. BUILD_SPEC §14: `GET /signals?acted_on=&limit=`.

Carries enough for a UI to show the recommendation plainly without a second
round trip for the owning strategy's name/slug — see `routes_signals.py`'s
join.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class ConditionOut(BaseModel):
    name: str
    description: str
    operator: str
    threshold: float
    actual: float
    passed: bool


class SignalOut(BaseModel):
    id: UUID
    symbol: str
    ts: datetime
    side: str
    intent: str
    rule_id: str
    rule_text: str
    features: dict
    conditions: list[ConditionOut]
    confidence: Decimal | None
    acted_on: bool
    veto_reason: str | None
    strategy_id: UUID
    strategy_slug: str
    strategy_name: str
