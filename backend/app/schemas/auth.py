"""Auth API schemas. BUILD_SPEC §14."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

# Deliberately a plain constrained `str`, not `pydantic.EmailStr` — that type
# requires the optional `email-validator` package, which isn't a project
# dependency (pyproject.toml is shared with other in-flight work tonight and
# not this task's to edit). A basic shape check is enough for a family tool;
# real validation happens implicitly via the unique-email constraint at
# registration.
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255, pattern=_EMAIL_PATTERN)
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: UUID
    display_name: str | None
