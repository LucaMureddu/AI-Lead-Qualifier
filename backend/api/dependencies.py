"""
api/dependencies.py
-------------------
JWT RS256 authentication dependency — V2.

Replaces api/security.py (JWT HS256 symmetric) with asymmetric RS256.
The public key is loaded from disk once and cached for the process lifetime.
The private key is used only by the /token mock endpoint (dev/test).

Environment variables
---------------------
JWT_PUBLIC_KEY_PATH  — path to RSA public key PEM file (validation)
JWT_PRIVATE_KEY_PATH — path to RSA private key PEM file (/token mock only)
"""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import get_settings

_bearer = HTTPBearer(auto_error=True)


@lru_cache(maxsize=1)
def _load_public_key() -> str:
    """Read the RSA public key from disk — cached for the process lifetime."""
    path: Path = get_settings().jwt_public_key_path
    try:
        return path.read_text()
    except FileNotFoundError:
        raise RuntimeError(
            f"JWT public key not found at '{path}'. "
            "Generate keys with: openssl genrsa -out keys/private.pem 2048 && "
            "openssl rsa -in keys/private.pem -pubout -out keys/public.pem"
        ) from None


@lru_cache(maxsize=1)
def _load_private_key() -> str:
    """Read the RSA private key from disk — cached for the process lifetime."""
    path: Path = get_settings().jwt_private_key_path
    try:
        return path.read_text()
    except FileNotFoundError:
        raise RuntimeError(
            f"JWT private key not found at '{path}'. "
            "Used only by the /token mock endpoint (dev/test)."
        ) from None


async def get_current_tenant_id(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    FastAPI dependency — validate the RS256 JWT and return the tenant_id.

    The ``sub`` claim is used as the tenant identifier. Raises HTTP 401 on
    missing, expired, or invalid tokens.
    """
    _401 = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token JWT non valido o scaduto.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload: dict = jwt.decode(
            credentials.credentials,
            _load_public_key(),
            algorithms=["RS256"],
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


def create_access_token(tenant_id: str, expires_delta_seconds: int = 3600) -> str:
    """
    Sign and return a JWT RS256 token for the given tenant_id.

    Uses the private key — for dev/test /token endpoint only.
    In production, tokens are issued by an external IdP.
    """
    now = int(time.time())
    payload: dict = {
        "sub": tenant_id,
        "iat": now,
        "exp": now + expires_delta_seconds,
    }
    return jwt.encode(payload, _load_private_key(), algorithm="RS256")
