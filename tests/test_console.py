"""The operator console: the gate, CSRF, and the rules a web app could erode.

The console can start crawls, spend model quota and publish a page to a real
contractor. Everything that protects against that being done accidentally, or
by someone else's page, is pinned here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.console import views
from app.copy_rules import contains_forbidden_dash

SECRET = "console-secret"


@pytest.fixture()
def client(monkeypatch):
    import app.console.auth as auth
    import app.console.routes as routes

    monkeypatch.setattr(auth, "get_config", lambda: Config(worker_shared_secret=SECRET))
    monkeypatch.setattr(routes, "publish_job", lambda *a, **k: "msg-1")
    monkeypatch.setattr(routes.jobs, "active", lambda: [])
    monkeypatch.setattr(routes.jobs, "recent", lambda n=40: [])
    monkeypatch.setattr(routes.jobs, "create", lambda kind, params, **kw: "job-1")
    monkeypatch.setattr(routes.store, "batch_overview", lambda days=14: [])

    from app.worker import app

    return TestClient(app, raise_server_exceptions=False)


def sign_in(client: TestClient) -> str:
    """Establish a session and return the CSRF token the server would embed."""
    client.get(f"/console?key={SECRET}", follow_redirects=False)
    return client.cookies.get("relay_csrf")


# ── The gate ──────────────────────────────────────────────────────────────────


def test_the_console_is_closed_without_a_key(client):
    assert client.get("/console").status_code == 401
    assert client.get("/console/jobs").status_code == 401
    assert client.get("/console/batches").status_code == 401


def test_a_wrong_key_is_refused(client):
    assert client.get("/console?key=nope").status_code == 401
    assert "relay_console" not in client.cookies


def test_the_key_becomes_a_session_and_leaves_the_url(client):
    first = client.get(f"/console?key={SECRET}", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/console"
    assert "relay_console" in first.cookies and "relay_csrf" in first.cookies

    page = client.get("/console")
    assert page.status_code == 200
    assert SECRET not in page.text, "the secret never reaches the page"


def test_a_rotated_secret_invalidates_the_session(client, monkeypatch):
    sign_in(client)
    assert client.get("/console").status_code == 200

    import app.console.auth as auth

    monkeypatch.setattr(auth, "get_config", lambda: Config(worker_shared_secret="rotated"))
    assert client.get("/console").status_code == 401


# ── CSRF ──────────────────────────────────────────────────────────────────────


def test_a_post_without_a_csrf_token_is_refused(client):
    sign_in(client)
    r = client.post("/console/sweep", data={"market": "Colorado Springs", "limit": 10},
                    follow_redirects=False)
    assert r.status_code == 403


def test_a_post_with_the_wrong_csrf_token_is_refused(client):
    sign_in(client)
    r = client.post("/console/sweep",
                    data={"market": "Colorado Springs", "limit": 10, "csrf": "forged"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_a_valid_post_starts_a_job_and_redirects_to_it(client):
    csrf = sign_in(client)
    r = client.post("/console/sweep",
                    data={"market": "Colorado Springs", "limit": 10, "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/console/jobs/job-1"


@pytest.mark.parametrize(
    "path, payload",
    [
        ("/console/agent", {"prompt": "sweep"}),
        ("/console/dispatch", {"batch_id": "b1", "market": "Colorado Springs", "limit": 5}),
        ("/console/draft", {"batch_id": "b1", "top": 3}),
        ("/console/suppress", {"value": "p1", "match_type": "place_id", "reason": "asked"}),
    ],
)
def test_every_mutating_route_demands_csrf(client, path, payload):
    sign_in(client)
    assert client.post(path, data=payload, follow_redirects=False).status_code == 403


def test_mutating_routes_are_closed_to_a_stranger(client):
    """No session: the gate answers before the body is even parsed.

    With the check inside each handler this returned 422, because FastAPI
    validates a form before calling the endpoint, so a stranger learned the
    field names. The gate is middleware for that reason.
    """
    for path in ("/console/sweep", "/console/agent", "/console/draft"):
        assert client.post(path, data={}, follow_redirects=False).status_code == 401


def test_every_console_route_is_gated(client):
    """Structural: enumerate the router and prove none of them answer without
    a session. Catches a route added later whose author forgot the gate."""
    from app.console.routes import router

    for route in router.routes:
        path = getattr(route, "path", "")
        if not path:
            continue
        concrete = path.replace("{job_id}", "j1").replace("{batch_id}", "b1") \
                       .replace("{audit_id}", "a1")
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            response = client.request(method, concrete, follow_redirects=False)
            assert response.status_code == 401, f"{method} {concrete} answered "\
                                                f"{response.status_code} without a session"


# ── The rules a web app could erode ───────────────────────────────────────────


def test_there_is_no_send_route(client):
    """Rule 4: drafts only, no automated sending. A button is how that erodes."""
    from app.console.routes import router

    paths = " ".join(getattr(r, "path", "") for r in router.routes).lower()
    for word in ("send", "email", "outreach", "message"):
        assert word not in paths, f"a {word} route exists in the console"


def test_no_console_template_offers_to_send_anything():
    import re

    page = views.render_run(csrf="t", markets=["Colorado Springs"],
                            active_jobs=[], recent_batches=[])
    flat = re.sub(r"\s+", " ", page.lower())
    assert "drafted by a model and approved by a human" in flat
    assert "nothing here sends a message" in flat


def test_approving_is_presented_as_the_human_act():
    audit = {"audit_id": "a1", "scores": {}, "batch_id": "b1"}
    findings = {"status": "draft", "needs_review": False, "findings": [
        {"ordinal": 1, "what_we_saw": "x", "what_it_means": "y", "what_fixing_takes": "z"}
    ]}
    page = views.render_audit(audit=audit, prospect={"business_name": "Peak"},
                              checks=[], definitions={}, findings=findings,
                              evidence=[], csrf="t")
    assert "Approve these three" in page
    assert "human selection the rules require" in page
    assert "Nothing is published by approving" in page


def test_a_flagged_draft_warns_before_approval():
    findings = {"status": "draft", "needs_review": True, "findings": []}
    page = views.render_audit(audit={"audit_id": "a1", "scores": {}}, prospect={},
                              checks=[], definitions={}, findings=findings,
                              evidence=[], csrf="t")
    assert "may name a" in page and "mechanism" in page


def test_suppression_asks_before_it_acts():
    page = views.render_audit(audit={"audit_id": "a1", "scores": {}, "prospect_id": "p1"},
                              prospect={"business_name": "Peak"}, checks=[],
                              definitions={}, findings=None, evidence=[], csrf="t")
    assert "confirm(" in page
    assert "cannot be undone" in page


# ── Copy rules apply to the console too ───────────────────────────────────────


def test_no_console_view_contains_a_forbidden_dash():
    pages = [
        views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[]),
        views.render_jobs([]),
        views.render_batches([]),
        views.render_batch("b1", [], {}, csrf="t"),
        views.render_audit(audit={"audit_id": "a1", "scores": {}}, prospect={},
                           checks=[], definitions={}, findings=None, evidence=[], csrf="t"),
    ]
    for page in pages:
        assert not contains_forbidden_dash(page)


def test_console_pages_are_noindex(client):
    sign_in(client)
    assert client.get("/console").headers["x-robots-tag"] == "noindex, nofollow"


def test_business_names_are_escaped():
    page = views.render_batch("b1", [{
        "rank": 1, "audit_id": "a1", "business_name": "<script>alert(1)</script>",
        "city": "COS", "segment": "Leaky Bucket", "scores": {}, "phone": "",
        "partial": False,
    }], {"Leaky Bucket": 1}, csrf="t")
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
