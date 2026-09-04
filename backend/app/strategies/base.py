"""The strategy interface. BUILD_SPEC §8.1, §8.2.

`Condition` is the load-bearing record here: every strategy must emit the
*complete* condition list, including conditions that failed, so the
education layer can explain not just "why it entered" but "how close it
came to not entering." A strategy that only reports the conditions it
happened to satisfy is not following the spec, no matter how correct its
final decision is.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.execution.positions import OpenPosition
from app.ingest.bars import FinalBar
from app.strategies.indicators import IndicatorSnapshot

Side = Literal["buy", "sell"]
Intent = Literal["entry", "exit"]


@dataclass(frozen=True)
class Condition:
    name: str  # 'rsi_below_threshold'
    description: str  # 'RSI(2) is below the oversold threshold'
    operator: str  # '<'
    threshold: float
    actual: float
    passed: bool


@dataclass(frozen=True)
class Signal:
    side: Side
    intent: Intent
    symbol: str
    rule_id: str
    rule_text: str
    features: dict[str, float | str | bool | None]  # everything examined
    conditions: list[Condition]  # full list, including failures
    stop_price: Decimal | None  # REQUIRED for entries — CLAUDE.md rule 6
    target_price: Decimal | None = None
    confidence: float = 0.5


@dataclass(frozen=True)
class BarContext:
    """Everything a strategy's evaluate()/manage() sees for one finalized bar.

    `history`/`indicators` are in the strategy's own primary timeframe
    (`Strategy.timeframe`). `daily_history`/`daily_indicators` are only
    populated for strategies whose primary timeframe isn't already daily —
    EMA crossover and RSI(2) both need a daily regime/trend filter
    regardless of what timeframe they trade on (BUILD_SPEC §8.3).
    """

    symbol: str
    bar: FinalBar
    history: list[FinalBar] = field(repr=False)
    indicators: IndicatorSnapshot
    daily_history: list[FinalBar] | None = field(default=None, repr=False)
    daily_indicators: IndicatorSnapshot | None = None


class Strategy(ABC):
    slug: str
    # The bar timeframe evaluate()/manage() are called with. BUILD_SPEC §8.3
    # specifies this per strategy (1Min for ORB/VWAP reversion, 5Min for EMA
    # crossover, 1Day for RSI(2)) — it's not a free choice per instance.
    timeframe: str
    default_params: dict

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    @abstractmethod
    def evaluate(self, ctx: BarContext) -> Signal | None:
        """Called on every finalized bar. Returns an entry Signal, or None."""
        ...

    @abstractmethod
    def manage(self, ctx: BarContext, position: OpenPosition) -> Signal | None:
        """Called each bar for an open position — trailing stops, time exits,
        target/stop touches. Returns an exit Signal, or None."""
        ...
