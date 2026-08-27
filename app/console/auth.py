"""The console gate: a session cookie plus a double submit CSRF token.

The console can start crawls, spend model quota and publish a page to a real
contractor, so it needs more than the read only dashboard's gate.

Session: visit once with ?key=<CONSOLE_PASSWORD>, receive an HttpOnly
cookie holding a hash of the password, and get redirected to a clean URL so it
never sits in the address bar. Changing the password invalidates every
existing cookie because the hash no longer matches.

CONSOLE_PASSWORD is deliberately separate from WORKER_SHARED_SECRET. The
latter authenticates Pub/Sub's server to server pushes and is a generated
token nobody types; this one is what a person enters, so it can be a password
the operator picked and remembers.

CSRF: a random token in a second HttpOnly cookie, echoed into every mutating
form by the server. An attacker's page can submit a form to us but cannot read
our cookie, so it cannot produce a matching field. SameSite=Lax closes the
rest.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from app.config import get_config

SESSION_COOKIE = "relay_console"
CSRF_COOKIE = "relay_csrf"
SESSION_HOURS = 12


def session_token() -> str:
    secret = get_config().console_password
    return hashlib.sha256(f"console:{secret}".encode()).hexdigest() if secret else ""


def _https(request: Request) -> bool:
    # Cloud Run terminates TLS and forwards http, so the header is the truth.
    return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"


def _set_cookies(response: Response, request: Request, csrf: str) -> None:
    secure = _https(request)
    response.set_cookie(SESSION_COOKIE, session_token(), httponly=True, secure=secure,
                        max_age=SESSION_HOURS * 3600, samesite="lax")
    response.set_cookie(CSRF_COOKIE, csrf, httponly=True, secure=secure,
                        max_age=SESSION_HOURS * 3600, samesite="lax")


def authorize(request: Request) -> Response | None:
    """None when the session is good, otherwise the response to return."""
    expected = session_token()
    if not expected:
        return None  # no secret configured: local development

    key = request.query_params.get("key")
    if key is not None:
        if hmac.compare_digest(key, get_config().console_password):
            response = RedirectResponse(url=request.url.path, status_code=303)
            _set_cookies(response, request, secrets.token_urlsafe(24))
            return response
        return Response(status_code=401)

    if hmac.compare_digest(request.cookies.get(SESSION_COOKIE) or "", expected):
        return None
    return Response(status_code=401)


def csrf_token(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or ""


def check_csrf(request: Request, submitted: str | None) -> bool:
    """Double submit: the form field must match the cookie."""
    if not get_config().console_password:
        return True
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    return bool(cookie) and bool(submitted) and hmac.compare_digest(cookie, submitted)
