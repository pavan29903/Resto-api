"""Owner authentication.

Supabase Auth owns signup, login, password resets and sessions. This module
only answers one question on each request: *which owner is this?*

Tokens are signed with an asymmetric key (ES256), so we verify them against
Supabase's public JWKS endpoint. The signing key never leaves Supabase, and
this backend holds no secret capable of minting a session.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.models import Owner

_jwks_client: PyJWKClient | None = None


def _jwks(settings: Settings) -> PyJWKClient:
    """Cached JWKS client — it refetches keys only when it sees an unknown
    `kid`, so key rotation is handled without a redeploy."""
    global _jwks_client
    if _jwks_client is None:
        if not settings.supabase_url:
            raise RuntimeError("SUPABASE_URL is not set.")
        _jwks_client = PyJWKClient(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
        )
    return _jwks_client


@dataclass(frozen=True)
class AuthUser:
    """The verified identity carried by the request's token."""

    id: str
    email: str


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def verify_token(token: str, settings: Settings) -> AuthUser:
    try:
        signing_key = _jwks(settings).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            issuer=f"{settings.supabase_url.rstrip('/')}/auth/v1",
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Your session expired. Sign in again."
        ) from exc
    except (jwt.InvalidTokenError, PyJWKClientError) as exc:
        # PyJWKClientError covers malformed headers and tokens claiming a key
        # we've never issued — including the classic `alg: none` forgery. Both
        # are a failed sign-in, not a server fault, so they must not 500.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "That sign-in could not be verified."
        ) from exc

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token is missing a subject.")
    return AuthUser(id=user_id, email=claims.get("email") or "")


async def current_owner(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Owner:
    """FastAPI dependency: the signed-in owner, created on first sight.

    Supabase already verified the email, so there is no separate registration
    step here — the first authenticated request provisions the row.
    """
    user = verify_token(_bearer_token(request), settings)

    owner = (
        await session.execute(select(Owner).where(Owner.auth_user_id == user.id))
    ).scalar_one_or_none()

    if owner is None:
        owner = Owner(auth_user_id=user.id, email=user.email)
        session.add(owner)
        await session.flush()
    elif user.email and owner.email != user.email:
        owner.email = user.email  # they changed it in Supabase

    return owner
