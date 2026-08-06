"""Signed-cookie session authentication. Railway owns the secrets."""

import secrets

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from waypoint.settings import Settings

COOKIE_NAME = "pf_session"
SESSION_MAX_AGE_SECONDS = 43200


def _signer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SESSION_KEY.get_secret_value())


def login(settings: Settings, response: Response, password: str) -> None:
    if not secrets.compare_digest(password, settings.APP_PASSWORD.get_secret_value()):
        # No WWW-Authenticate header: this is a cookie login, not basic auth.
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response.set_cookie(
        COOKIE_NAME,
        _signer(settings).dumps({"authenticated": True}),
        secure=True,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )


def require_session(request: Request) -> None:
    settings: Settings = request.app.state.settings
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        _signer(settings).loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired) as error:
        raise HTTPException(status_code=401, detail="Session invalid or expired") from error
