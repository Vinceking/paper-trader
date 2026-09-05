"""Registration/login endpoints. BUILD_SPEC §14.

Scope for tonight (see task brief / CLAUDE.md): a single role ("requester")
for every family member, auto-login on register, no two-role
requester/approver machinery — that's ADDENDUM_LIVE_APPROVAL territory for a
later phase, not this one.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.auth.security import create_access_token, hash_password, verify_password
from app.deps import DbSession
from app.models.account import PaperAccount, User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

_STARTING_CASH = Decimal("100000.00")


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, db: DbSession) -> TokenResponse:
    existing = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    await db.flush()  # populate user.id before it's referenced below

    account = PaperAccount(
        user_id=user.id,
        name=f"{body.display_name}'s account",
        starting_cash=_STARTING_CASH,
        cash=_STARTING_CASH,
        equity=_STARTING_CASH,
    )
    db.add(account)
    await db.commit()

    token = create_access_token(user_id=user.id, role=user.role)
    return TokenResponse(
        access_token=token, user_id=user.id, display_name=user.display_name
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: DbSession) -> TokenResponse:
    user = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()

    # Generic "invalid credentials" either way — don't leak which part was wrong.
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")

    token = create_access_token(user_id=user.id, role=user.role)
    return TokenResponse(
        access_token=token, user_id=user.id, display_name=user.display_name
    )
