"""Bearer-token authentication.

Every endpoint except ``/healthz`` requires ``Authorization: Bearer <token>``.
The token comes from ``PYBOXD_AUTH_TOKEN`` and is compared in constant time
(``hmac.compare_digest``) so response timing leaks nothing about it.
"""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse

MIN_TOKEN_LENGTH = 16
TOKEN_ENV = "PYBOXD_AUTH_TOKEN"  # noqa: S105 (env var name, not a secret)


class AuthConfigError(RuntimeError):
    pass


def check_token_config(token: str | None) -> str:
    if not token or len(token) < MIN_TOKEN_LENGTH:
        raise AuthConfigError(
            f"{TOKEN_ENV} must be set to a secret of at least {MIN_TOKEN_LENGTH} characters"
        )
    return token


def unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"code": "unauthorized", "message": message}},
        headers={"WWW-Authenticate": 'Bearer realm="pyboxd"'},
    )


def verify(request: Request, expected: str) -> JSONResponse | None:
    """Return a 401 response if the request is not authenticated, else ``None``."""
    if not expected:
        return unauthorized("authentication is not configured on the server")
    header = request.headers.get("authorization", "")
    if not header:
        return unauthorized("missing Authorization header")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return unauthorized("malformed Authorization header; expected 'Bearer <token>'")
    if not hmac.compare_digest(token.strip().encode(), expected.encode()):
        return unauthorized("invalid authentication token")
    return None
