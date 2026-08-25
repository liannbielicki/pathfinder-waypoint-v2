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


def require_session_or_outcomes_token(request: Request) -> None:
    """Session cookie OR the outcomes machine token.

    Scoped deliberately to the outcome automation — POST /api/outcomes and
    GET /api/funnel/worklist, which is the shipped (run_id, pro_id) pairs and
    nothing more. It must not carry APP_PASSWORD, which
    is full operator access (kill switch, run creation, every run's detail) and
    which n8n stores in plaintext execution history.

    A malformed or wrong Bearer header is rejected outright rather than falling
    through to the cookie check — a caller that presented a token meant to
    authenticate with it, and silently downgrading would turn a leaked-token
    alarm into a confusing 401 from a different code path.
    """
    settings: Settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = settings.OUTCOMES_TOKEN
        if token is None:
            raise HTTPException(status_code=401, detail="Token auth is not configured")
        if not secrets.compare_digest(header[7:].strip(), token.get_secret_value()):
            raise HTTPException(status_code=401, detail="Invalid token")
        return
    require_session(request)
