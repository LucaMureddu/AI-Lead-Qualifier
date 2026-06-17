"""
api/security.py
---------------
JWT-based multi-tenant authentication for the FastAPI application.

Every protected endpoint injects ``tenant_id`` via::

    tenant_id: str = Depends(get_current_tenant_id)

The token must be sent as::

    Authorization: Bearer <signed-jwt>

Token payload expected (minimum)::

    {"sub": "<tenant_id>", ...}

The ``sub`` claim is used as ``tenant_id``.  Additional claims (``exp``,
``iat``) are validated automatically by PyJWT when present.

Environment variables
---------------------
JWT_SECRET_KEY  — HMAC-SHA256 signing secret (required in production).
                  Defaults to a dev-only placeholder that MUST be overridden
                  before deploying.
JWT_ALGORITHM   — Algorithm string (default: ``HS256``).
"""

from __future__ import annotations

import os
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ── Configuration ─────────────────────────────────────────────────────────────

_DEV_SECRET = "dev-secret-change-in-production!"  # noqa: S105  (intentional placeholder — 32 bytes min per RFC 7518)

SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", _DEV_SECRET)
ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

# ── Bearer scheme ─────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=True)


# ── Dependency ────────────────────────────────────────────────────────────────

async def get_current_tenant_id(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> str:
    """
    FastAPI dependency — decode the JWT and return the ``tenant_id``.

    Raises HTTP 401 if the token is missing, malformed, expired, or unsigned
    with the expected key.  The ``sub`` claim is used as the tenant identifier.
    """
    _401 = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token JWT non valido o scaduto.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload: dict = jwt.decode(
            credentials.credentials,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token JWT scaduto.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.PyJWTError:
        raise _401 from None

    tenant_id: str | None = payload.get("sub")
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token JWT privo del claim 'sub' (tenant_id).",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return tenant_id


# ── Token generation helper (used by /token endpoint and tests) ───────────────

def create_access_token(tenant_id: str, expires_delta_seconds: int = 3600) -> str:
    """
    Sign and return a JWT for the given ``tenant_id``.

    Parameters
    ----------
    tenant_id:
        Value embedded in the ``sub`` claim.
    expires_delta_seconds:
        Token lifetime in seconds (default 1 hour).  Pass ``None`` for a
        token with no expiry (useful in tests).
    """
    import time

    payload: dict = {"sub": tenant_id, "iat": int(time.time())}
    if expires_delta_seconds is not None:
        payload["exp"] = int(time.time()) + expires_delta_seconds

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
