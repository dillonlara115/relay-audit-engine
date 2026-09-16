"""The post-deploy smoke check, driven by a mock transport.

A smoke test that cannot fail is decoration, so most of these prove it notices
when the deployment is wrong. The specific regressions it exists to catch are
named: a stripped cookie, a routeless bare hostname, a gate that answers with
no form in it.
"""

from __future__ import annotations

import httpx
import pytest

from app.smoke import SESSION_COOKIE, run

BASE = "https://reports.example.com"
PASSWORD = "hunter2"

CONSOLE_PAGE = "<html><body>Relay Audit Engine, call list</body></html>" + "x" * 2000
LOGIN_PAGE = '<html><body><form action="/console/login"><input type="password"></form></body></html>'
REPORT_PAGE = "<html><body>what a homeowner finds</body></html>" + "x" * 2000

NOINDEX = {"x-robots-tag": "noindex, nofollow"}


def healthy(**overrides):
    """A deployment where everything works. Overrides break one thing."""
    behaviour = {
        "/health": lambda: httpx.Response(200, json={"ok": True}, headers=NOINDEX),
        "/robots.txt": lambda: httpx.Response(200, text="User-agent: *\nDisallow: /",
                                              headers=NOINDEX),
        "/console": lambda signed: (httpx.Response(200, text=CONSOLE_PAGE, headers=NOINDEX)
                                    if signed else
                                    httpx.Response(401, text=LOGIN_PAGE, headers=NOINDEX)),
        "/console/batches": lambda signed: httpx.Response(200 if signed else 401,
                                                          text=CONSOLE_PAGE, headers=NOINDEX),
        "/": lambda signed: httpx.Response(303 if signed else 401, text=LOGIN_PAGE,
                                           headers={**NOINDEX, "location": "/console"}),
        "login_status": 303,
        "cookie_name": SESSION_COOKIE,
        "report": httpx.Response(200, text=REPORT_PAGE, headers=NOINDEX),
        "unknown_slug": httpx.Response(404, text="", headers=NOINDEX),
    }
    behaviour.update(overrides)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        signed = SESSION_COOKIE in request.headers.get("cookie", "")

        if request.method == "POST" and path == "/console/login":
            status = behaviour["login_status"]
            response = httpx.Response(status, headers={**NOINDEX, "location": "/console"},
                                      text=LOGIN_PAGE)
            if status == 303 and behaviour["cookie_name"]:
                response.headers["set-cookie"] = (
                    f"{behaviour['cookie_name']}=abc123; Path=/; HttpOnly")
            return response

        if path in ("/health", "/robots.txt"):
            return behaviour[path]()
        if path in ("/console", "/console/batches", "/"):
            return behaviour[path](signed)
        if path == "/" + "z" * 16:
            return behaviour["unknown_slug"]
        return behaviour["report"]

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def check(result, name):
    return next(c for c in result.checks if c.name == name)


# ── the happy path ────────────────────────────────────────────────────────────


def test_a_working_deployment_passes_everything():
    result = run(BASE, PASSWORD, slug="S8_n4NYrBhlviJJW", client=healthy())
    assert result.ok, [c.name for c in result.failed]
    assert len(result.checks) > 12


def test_the_slug_check_is_opt_in():
    """Fetching a report writes a view log entry, so it only runs when asked."""
    names = [c.name for c in run(BASE, PASSWORD, client=healthy()).checks]
    assert "a real report renders for a stranger" not in names


# ── the regressions it exists to catch ────────────────────────────────────────


def test_it_catches_a_cookie_that_never_comes_back():
    """Firebase Hosting strips every cookie except __session. This is the bug
    that shipped green: the password worked and the session never arrived."""
    result = run(BASE, PASSWORD, client=healthy(cookie_name="relay_console"))

    assert not result.ok
    failed = check(result, f"session rides in {SESSION_COOKIE}")
    assert failed.fatal is True
    assert "stripped by Firebase Hosting" in failed.detail


def test_it_catches_a_session_that_is_set_but_not_honoured():
    result = run(BASE, PASSWORD, client=healthy(
        **{"/console": lambda signed: httpx.Response(401, text=LOGIN_PAGE, headers=NOINDEX)}))

    assert not check(result, "the session is accepted on the next request").ok
    assert not check(result, "console renders, not the form again").ok


def test_it_catches_a_bare_hostname_with_no_route():
    """404 with a JSON body is what a person actually saw after signing in."""
    result = run(BASE, PASSWORD, client=healthy(
        **{"/": lambda signed: httpx.Response(500, text='{"detail":"Not Found"}')}))

    assert not check(result, "bare hostname is not an error").ok


def test_it_catches_a_gate_with_no_form_in_it():
    result = run(BASE, PASSWORD, client=healthy(
        **{"/console": lambda signed: (httpx.Response(200, text=CONSOLE_PAGE, headers=NOINDEX)
                                       if signed else httpx.Response(401, text="", headers=NOINDEX))}))

    assert not check(result, "gate shows a password form").ok


def test_it_catches_a_rejected_password():
    result = run(BASE, PASSWORD, client=healthy(login_status=401))

    assert not result.ok
    assert check(result, "password is accepted").fatal is True


def test_it_catches_a_missing_noindex_header():
    result = run(BASE, PASSWORD, client=healthy(
        **{"/robots.txt": lambda: httpx.Response(200, text="User-agent: *\nDisallow: /")}))

    assert not check(result, "every response carries noindex").ok


def test_it_catches_an_ungated_console():
    result = run(BASE, PASSWORD, client=healthy(
        **{"/console": lambda signed: httpx.Response(200, text=CONSOLE_PAGE, headers=NOINDEX)}))

    assert not check(result, "console is gated").ok


# ── it must not fall over ─────────────────────────────────────────────────────


def test_an_unreachable_host_is_a_failed_check_not_a_traceback():
    def refuse(request):
        raise httpx.ConnectError("nope")

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    result = run(BASE, PASSWORD, client=client)

    assert not result.ok
    assert result.checks[0].fatal is True
    assert "ConnectError" in result.checks[0].detail


def test_a_fatal_check_stops_the_rest():
    """No point asserting on pages served by something that is not the app."""
    result = run(BASE, PASSWORD, client=healthy(
        **{"/health": lambda: httpx.Response(503, headers=NOINDEX)}))

    assert len(result.checks) == 1
    assert not result.ok


def test_the_base_url_tolerates_a_trailing_slash():
    assert run(BASE + "/", PASSWORD, client=healthy()).base_url == BASE
