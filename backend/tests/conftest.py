"""Shared test fixtures.

DB-backed tests run against an in-memory SQLite database (via aiosqlite)
rather than the real Postgres+Timescale target, so the suite needs neither
Docker nor a running database. `StaticPool` keeps a single connection alive
for the lifetime of the engine so every session sees the same in-memory DB.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session

    await engine.dispose()
