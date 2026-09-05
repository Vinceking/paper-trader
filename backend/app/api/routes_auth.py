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
from app.models.strategies import StrategyRecord
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.strategies.registry import STRATEGIES

router = APIRouter(prefix="/auth", tags=["auth"])

_STARTING_CASH = Decimal("100000.00")

# Human-readable labels for the one auto-created StrategyRecord per known
# slug every new user gets (see below) -- STRATEGIES itself only maps a slug
# to its class, with no display-name attribute of its own.
_STRATEGY_LABELS: dict[str, str] = {
    "orb": "Opening Range Breakout",
    "vwap_reversion": "VWAP Reversion",
    "ema_cross": "EMA Crossover",
    "rsi2": "RSI(2) Mean Reversion",
}


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

    # One StrategyRecord per known slug, disabled by default -- this is what
    # gives the new account something for the live ingest pipeline
    # (app/ingest/pipeline.py) to actually evaluate against real bars, so
    # GET /signals has recommendations to show once the market's open,
    # without the user needing to call POST /strategies by hand first.
    # `enabled=False`/`gate_passed=False`: recording a signal never requires
    # either (see SignalRecord's own design) -- only auto-*placing an order*
    # would, and nothing in this codebase does that yet regardless.
    for slug in STRATEGIES:
        db.add(StrategyRecord(
            user_id=user.id,
            slug=slug,
            name=_STRATEGY_LABELS.get(slug, slug),
            params={},
        ))

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
