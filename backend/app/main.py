"""FastAPI application. BUILD_SPEC §14.

This is the `api` process. It does NOT own the market data socket — that lives in
the single-instance `ingest` process (§4). Running the websocket consumer inside a
horizontally-scaled API server duplicates the connection and races on bar building.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import (
    routes_account,
    routes_auth,
    routes_health,
    routes_market,
    routes_orders,
    routes_strategies,
)
from app.config import get_settings

# Resolved relative to this file, not the process's working directory — robust
# whether run locally (`uvicorn app.main:app`) or bundled by Vercel, where the
# working directory convention differs (see Vercel's Python runtime docs).
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

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
    app.include_router(routes_auth.router)
    app.include_router(routes_account.router)
    app.include_router(routes_strategies.router)

    # Mounted LAST and at "/" so it only ever catches paths none of the API
    # routers above matched — the stopgap frontend (see ../static), served
    # same-origin so the frontend's relative fetch() calls need no CORS
    # config. `html=True` serves static/index.html for "/" and any
    # unmatched sub-path, which is what a client-side-routed SPA needs.
    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


app = create_app()
