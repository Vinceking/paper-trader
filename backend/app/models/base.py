from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def uuid_pk() -> Mapped[uuid.UUID]:
    """Dialect-portable UUID primary key.

    `sqlalchemy.Uuid` renders as native UUID on Postgres and CHAR(32) on
    SQLite, so the same models run against production Postgres and an
    in-memory SQLite test database without a Docker/Postgres dependency.
    """
    return mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


def created_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
