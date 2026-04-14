"""Shared FastAPI dependencies for dashboard routes.

Centralizes the Bearer-token check so every route module can apply it
without re-declaring the auth plumbing. The token is read from
``request.app.state.auth_token`` (populated by ``create_app``), making
the dependency test-friendly (``create_app(token=...)`` factory already
works).
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.status import HTTP_401_UNAUTHORIZED

_security = HTTPBearer(auto_error=False)
_security_dep = Depends(_security)


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _security_dep,  # noqa: B008
) -> str:
    """Verify a dashboard authentication Bearer token.

    Raises:
        HTTPException: 401 when the header is missing or the token does
            not match ``request.app.state.auth_token`` (constant-time
            comparison).
    """
    if credentials is None:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    expected = request.app.state.auth_token
    if not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return credentials.credentials
