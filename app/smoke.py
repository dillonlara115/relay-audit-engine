"""Does the deployed thing actually work, from outside it.

Three bugs in the login shipped green: a cookie Firebase Hosting strips before
Cloud Run ever sees it, a bare hostname with no route behind it, and a next=
that pointed at that route. The suite passed through all of them, because none
of the failures exist in a world without a CDN in front and a browser in
front of that. They lived in the seam, and a seam has no unit test.

So this is deliberately not a unit test. It talks to the real URL over the
real network, through whatever proxy is in the way, signs in with the real
password, and asserts on what comes back. Every check names what broke rather
than which assertion failed, because the point of running it is to find out
what to fix.

Read only. It signs in, reads pages and signs out again. It publishes nothing,
sends nothing, and touches no prospect record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

SESSION_COOKIE = "__session"
TIMEOUT = 25.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = False   # nothing after this can be trusted, so stop


@dataclass
class Result:
    base_url: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return not self.failed

    def add(self, name: str, ok: bool, detail: str = "", *, fatal: bool = False) -> Check:
        check = Check(name, ok, detail, fatal)
        self.checks.append(check)
        return check


def _has_password_form(body: str) -> bool:
    return 'type="password"' in body and "/console/login" in body


def run(base_url: str, password: str, *, slug: str | None = None,
        client: httpx.Client | None = None) -> Result:
    """Walk the deployed surface. Never raises: a dead host is a failed check."""
    base = base_url.rstrip("/")
    result = Result(base_url=base)
    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)

    def get(path: str, **kw: Any) -> httpx.Response | None:
        try:
            return http.get(f"{base}{path}", **kw)
        except Exception as exc:  # noqa: BLE001 - an unreachable host is a result
            result.add(f"GET {path}", False, f"{type(exc).__name__}: {exc}"[:120], fatal=True)
            return None

    try:
        # ── open surface ──────────────────────────────────────────────────────
        health = get("/health")
        if health is None:
            return result
        result.add("health answers", health.status_code == 200,
                   f"got {health.status_code}", fatal=health.status_code != 200)
        if result.checks[-1].fatal:
            return result

        robots = get("/robots.txt")
        result.add("robots.txt refuses crawlers",
                   bool(robots) and robots.status_code == 200
                   and "Disallow: /" in robots.text,
                   f"got {robots.status_code if robots else 'nothing'}")

        # ── the gate, signed out ──────────────────────────────────────────────
        console = get("/console")
        result.add("console is gated", bool(console) and console.status_code == 401,
                   f"got {console.status_code if console else 'nothing'}")
        result.add("gate shows a password form",
                   bool(console) and _has_password_form(console.text),
                   "no form in the body, so a person cannot sign in")
        result.add("gate does not trigger the browser dialog",
                   bool(console) and "www-authenticate" not in
                   {k.lower() for k in console.headers},
                   "a WWW-Authenticate header replaces the page with basic auth")

        root = get("/")
        result.add("bare hostname is not an error",
                   bool(root) and root.status_code in (401, 404, 303),
                   f"got {root.status_code if root else 'nothing'}, "
                   "which means no route and a raw Not Found")

        # ── signing in, the part that only breaks in production ───────────────
        try:
            login = http.post(f"{base}/console/login",
                              data={"password": password, "next": "/console"})
        except Exception as exc:  # noqa: BLE001
            result.add("sign in", False, f"{type(exc).__name__}: {exc}"[:120], fatal=True)
            return result

        signed = login.status_code == 303
        result.add("password is accepted", signed,
                   f"got {login.status_code}, expected a redirect", fatal=not signed)
        if not signed:
            return result

        cookie = login.cookies.get(SESSION_COOKIE)
        result.add(f"session rides in {SESSION_COOKIE}", bool(cookie),
                   "a differently named cookie is stripped by Firebase Hosting "
                   "and never reaches Cloud Run", fatal=not cookie)
        if not cookie:
            return result

        # Set on the client rather than per request: httpx is deprecating the
        # per-request form, and a jar on the client is closer to a browser.
        http.cookies.set(SESSION_COOKIE, cookie)

        # The check that would have caught the cookie bug.
        after = get("/console")
        landed = bool(after) and after.status_code == 200
        result.add("the session is accepted on the next request", landed,
                   f"got {after.status_code if after else 'nothing'}: signed in, "
                   "then immediately asked for the password again")
        result.add("console renders, not the form again",
                   bool(after) and not _has_password_form(after.text),
                   "the login form came back")

        deep = get("/console/batches")
        result.add("a deep link works while signed in",
                   bool(deep) and deep.status_code == 200,
                   f"got {deep.status_code if deep else 'nothing'}")

        home = get("/")
        result.add("bare hostname leads somewhere while signed in",
                   bool(home) and home.status_code in (200, 303),
                   f"got {home.status_code if home else 'nothing'}")

        # ── the public surface stays public, and nothing else does ────────────
        missing = get("/" + "z" * 16)
        result.add("an unknown report is a 404",
                   bool(missing) and missing.status_code == 404,
                   f"got {missing.status_code if missing else 'nothing'}")

        if slug:
            report = get(f"/{slug}")
            rendered = bool(report) and report.status_code == 200 and len(report.text) > 1500
            result.add("a real report renders for a stranger", rendered,
                       f"got {report.status_code if report else 'nothing'}, "
                       f"{len(report.text) if report else 0} bytes")
            result.add("the report is not indexable",
                       bool(report) and "noindex" in
                       (report.headers.get("x-robots-tag") or ""),
                       "a page naming a contractor's problems must not be indexed")

        everything = [c for c in (health, robots, console, missing) if c is not None]
        result.add("every response carries noindex",
                   all("noindex" in (r.headers.get("x-robots-tag") or "")
                       for r in everything),
                   "something is missing the header the middleware should always add")
        return result
    finally:
        if owned:
            http.close()
