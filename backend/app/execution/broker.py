"""The Broker interface. BUILD_SPEC §7.5.

`PaperBroker` is the only implementation, ever (CLAUDE.md rule 1). This ABC
exists so a live driver *could* be added later by an adult with their own
funded account — it is not implemented here, and nothing in this build wires
one up.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.execution.friction import FillResult

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop"]


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType = "market"
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    # 'filled' | 'partial' | 'rejected' | 'cancelled'
    status: str
    submitted_at: datetime
    filled_at: datetime | None
    # The friction-adjusted fill, per §9. None only when the order was not
    # filled at all (e.g. rejected).
    fill: FillResult | None


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal


@dataclass(frozen=True)
class BrokerAccount:
    cash: Decimal
    equity: Decimal


class Broker(ABC):
    @abstractmethod
    async def submit(self, order: OrderRequest) -> BrokerOrder: ...

    @abstractmethod
    async def cancel(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_account(self) -> BrokerAccount: ...
