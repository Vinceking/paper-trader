"""Shared FastAPI dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.execution.alpaca_trading_client import AlpacaPaperTradingClient
from app.execution.paper_broker import AlpacaTradingClient

DbSession = Annotated[AsyncSession, Depends(get_session)]


@lru_cache
def get_alpaca_client() -> AlpacaTradingClient:
    return AlpacaPaperTradingClient(get_settings())


AlpacaClient = Annotated[AlpacaTradingClient, Depends(get_alpaca_client)]


def get_now() -> datetime:
    """The current instant, as a dependency so tests can override it.

    Routes that stamp `now()` directly are effectively untestable around
    time-sensitive logic (e.g. the risk engine's near-close veto) without
    the test's pass/fail depending on what time it happens to be when the
    suite runs.
    """
    return datetime.now(UTC)


Clock = Annotated[datetime, Depends(get_now)]
