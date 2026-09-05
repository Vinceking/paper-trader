"""Password hashing and JWT utilities for the family-login auth path.

Kept deliberately small for tonight's scope (CLAUDE.md / task brief): one
role ("requester") for everyone, a single long-lived access token, no
refresh-token machinery. See app/api/routes_auth.py for how these are used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _pwd_context.verify(password, hashed)


def create_access_token(
    user_id: UUID, role: str, expires_minutes: int = 60 * 24 * 7
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    settings = get_settings()
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate an access token.

    Lets `jose.JWTError` (covers expired-signature and any malformed/invalid
    token) propagate to the caller — `app.deps.get_current_user` catches it
    and turns it into a 401.
    """
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError:
        raise
