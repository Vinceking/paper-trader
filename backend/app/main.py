"""FastAPI application. BUILD_SPEC §14.

This is the `api` process. It does NOT own the market data socket — that lives in
the single-instance `ingest` process (§4). Running the websocket consumer inside a
horizontally-scaled API server duplicates the connection and races on bar building.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api import routes_health, routes_market, routes_orders
from app.config import get_settings

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # raises if the paper-only rails are violated
    log.info(
        "api.startup",
        environment=settings.environment,
        data_feed=settings.alpaca_data_feed,
        live_trading=settings.enable_live_trading,
    )
    if settings.alpaca_data_feed == "iex":
        log.warning(
            "data.partial_tape",
            detail="IEX is a partial tape, not the SIP consolidated feed. "
                   "Fill prices are approximations biased optimistic.",
        )
    yield
    log.info("api.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Paper Trader",
        version="0.1.0",
        description=(
            "Simulated day trading with a live market data feed and a post-trade "
            "education layer. This service never places real orders."
        ),
        lifespan=lifespan,
    )
    app.include_router(routes_health.router)
    app.include_router(routes_market.router)
    app.include_router(routes_orders.router)
    return app


app = create_app()
