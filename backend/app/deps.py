"""Shared FastAPI dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token
from app.config import get_settings
from app.db import get_session
from app.execution.alpaca_trading_client import AlpacaPaperTradingClient
from app.execution.paper_broker import AlpacaTradingClient
from app.models.account import PaperAccount, User

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


# ---- Auth (added for the family-login task; see app/auth/security.py) -------

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    """Extract and validate the Bearer token, load the User. 401 on any failure."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="not authenticated")

    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc

    raw_user_id = payload.get("sub")
    if raw_user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        user_id = UUID(raw_user_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_user_account(
    db: DbSession,
    user: CurrentUser,
) -> PaperAccount:
    """Load the current user's (single, for tonight's scope) PaperAccount."""
    account = (
        await db.execute(select(PaperAccount).where(PaperAccount.user_id == user.id))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=500, detail="user has no paper account")
    return account


CurrentUserAccount = Annotated[PaperAccount, Depends(get_current_user_account)]
