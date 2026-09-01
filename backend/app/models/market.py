"""Market data tables. BUILD_SPEC §5."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, uuid_pk


class Bar(Base):
    """OHLCV bar. Timestamp is BAR-OPEN time, always UTC.

    Only finalized bars are written here. See BUILD_SPEC §7.2 — evaluating a
    strategy on a forming bar is a lookahead bug.
    """

    __tablename__ = "bars"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    open: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    trade_count: Mapped[int | None] = mapped_column(Integer)

    # 'alpaca_iex' | 'alpaca_sip' | 'backfill' | 'replay'
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="alpaca_iex")

    __table_args__ = (
        Index("ix_bars_symbol_tf_ts_desc", "symbol", "timeframe", ts.desc()),
    )


class IngestState(Base):
    """Per-symbol ingest bookkeeping, powering /health and gap backfill.

    BUILD_SPEC §7.1: track last_bar_ts per symbol and expose it. A silent gap in
    the bar series corrupts every indicator downstream, so this table is how the
    system knows it has one.
    """

    __tablename__ = "ingest_state"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True, default="1Min")

    last_bar_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfilled_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gap_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GapEvent(Base):
    """A detected hole in the bar series, and whether it was filled.

    Append-only. If backfill fails, the row stays unresolved and /health reports
    it — the system must never pretend a gap did not happen.
    """

    __tablename__ = "gap_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1Min")
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gap_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_bars: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved: Mapped[bool] = mapped_column(nullable=False, default=False)
    detail: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = created_at_col()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
