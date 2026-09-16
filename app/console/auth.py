"""The console gate: a session cookie plus a double submit CSRF token.

The console can start crawls, spend model quota and publish a page to a real
contractor, so it needs more than the read only dashboard's gate.

Session: a form at the page you asked for. Enter the password, receive an
HttpOnly cookie holding a hash of it, and land on the page you were going to.
Changing the password invalidates every existing cookie because the hash no
longer matches.

The form is served with a 401 rather than a 200. A browser renders the body
either way, and the status stays honest for anything that is not a browser:
curl, a monitor, and the route tests all still see an unauthenticated request
refused. No WWW-Authenticate header, because that would trigger the browser's
own basic-auth dialog instead of the page.

?key=<CONSOLE_PASSWORD> still works for a bookmark or a script, and still
strips itself out of the address bar on the way through.

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

LOGIN_PATH = "/console/login"

# A wrong password costs a second. It will not stop somebody determined, but it
# turns an unthrottled guessing loop into a slow one, and the console can spend
# real money. Cloud Run scales to zero and runs many instances, so a counter in
# memory would be meaningless; the honest fix above this is Cloud Armor or IAP.
FAILED_ATTEMPT_DELAY_SECONDS = 1.0


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


def safe_next(raw: str | None, default: str = "/console") -> str:
    """A same-site path to return to after signing in, or the default.

    Anything that could leave this origin is discarded. A login form that
    honours an arbitrary next= is an open redirect, and an open redirect on a
    login page is how a convincing phish gets built.
    """
    candidate = (raw or "").strip()
    if (not candidate or not candidate.startswith("/") or candidate.startswith("//")
            or "\\" in candidate or candidate.startswith(LOGIN_PATH)):
        return default
    return candidate


def grant(request: Request, next_path: str) -> Response:
    """A signed-in session, landing on the page they were trying to reach."""
    response = RedirectResponse(url=safe_next(next_path), status_code=303)
    _set_cookies(response, request, secrets.token_urlsafe(24))
    return response


def password_matches(submitted: str | None) -> bool:
    secret = get_config().console_password
    return bool(secret) and bool(submitted) and hmac.compare_digest(submitted, secret)


def signed_in(request: Request) -> bool:
    expected = session_token()
    if not expected:
        return True  # no secret configured: local development
    return hmac.compare_digest(request.cookies.get(SESSION_COOKIE) or "", expected)


def login_response(request: Request, *, error: str | None = None,
                   next_path: str | None = None) -> Response:
    """The form, at the address they asked for."""
    from app.console.views import render_login

    body = render_login(next_path=safe_next(next_path or request.url.path), error=error)
    return Response(content=body, status_code=401,
                    media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "private, no-store",
                             "X-Robots-Tag": "noindex, nofollow"})


def authorize(request: Request) -> Response | None:
    """None when the session is good, otherwise the response to return."""
    if not session_token():
        return None  # no secret configured: local development

    key = request.query_params.get("key")
    if key is not None:
        if password_matches(key):
            return grant(request, request.url.path)
        return login_response(request, error="That password is not right.")

    if signed_in(request):
        return None
    return login_response(request)


def csrf_token(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or ""


def check_csrf(request: Request, submitted: str | None) -> bool:
    """Double submit: the form field must match the cookie."""
    if not get_config().console_password:
        return True
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    return bool(cookie) and bool(submitted) and hmac.compare_digest(cookie, submitted)
