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

    monkeypatch.setattr(auth, "get_config", lambda: Config(console_password=SECRET))
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

    monkeypatch.setattr(auth, "get_config", lambda: Config(console_password="rotated"))
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
    assert "written by a model, checked by you" in flat
    assert "never sent automatically" in flat


def test_approving_is_presented_as_the_human_act():
    audit = {"audit_id": "a1", "scores": {}, "batch_id": "b1"}
    findings = {"status": "draft", "needs_review": False, "findings": [
        {"ordinal": 1, "what_we_saw": "x", "what_it_means": "y", "what_fixing_takes": "z"}
    ]}
    page = views.render_audit(audit=audit, prospect={"business_name": "Peak"},
                              checks=[], definitions={}, findings=findings,
                              evidence=[], csrf="t")
    assert "These look right" in page
    assert "A person has to agree these are the right three" in page
    assert "does not send or publish" in page


def test_a_flagged_draft_warns_before_approval():
    findings = {"status": "draft", "needs_review": True, "findings": []}
    page = views.render_audit(audit={"audit_id": "a1", "scores": {}}, prospect={},
                              checks=[], definitions={}, findings=findings,
                              evidence=[], csrf="t")
    assert "Read this before approving" in page
    assert "never how we measured it" in page


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


# ── Batch filters and sorting ────────────────────────────────────────────────
#
# The batch page filters client side over data already on the page, so what
# matters server side is that the right data lands in the right attributes:
# per-row check statuses, sortable score values, and a filter dropdown built
# from the check definitions.


def _defs(*rows):
    base = {"points": 1, "enabled": True, "sort_order": 0}
    return [dict(base, **r) for r in rows]


def test_check_filter_options_are_grouped_by_section():
    defs = _defs(
        {"code": "C16", "title": "Footer copyright", "section": "chosen", "sort_order": 350},
        {"code": "B1", "title": "Self-serve booking", "section": "booked", "sort_order": 400},
    )
    page = views.render_batch("b1", [], {}, defs, csrf="t")
    assert '<optgroup label="Chosen">' in page
    assert '<optgroup label="Booked">' in page
    assert "C16: Footer copyright" in page
    assert "B1: Self-serve booking" in page


def test_the_copyright_example_is_findable_by_a_new_operator():
    """The concrete ask: find businesses with an old copyright year. C16 is
    the check that measures it, so the help text has to name it."""
    defs = _defs({"code": "C16", "title": "Footer copyright", "section": "chosen"})
    page = views.render_batch("b1", [], {}, defs, csrf="t")
    assert "old copyright year" in page
    assert "C16" in page


def test_each_row_carries_its_check_statuses_as_data():
    row = {"rank": 1, "audit_id": "a1", "business_name": "Peak Roofing", "city": "COS",
           "segment": "Leaky Bucket", "scores": {"found": 15, "chosen": 18, "booked": 0, "total": 44},
           "phone": "x", "partial": False, "checks": {"C16": "fail", "B1": "pass"}}
    page = views.render_batch("b1", [row], {"Leaky Bucket": 1}, [], csrf="t")
    assert ('data-checks="{&quot;C16&quot;:&quot;fail&quot;,'
            '&quot;B1&quot;:&quot;pass&quot;}"') in page


def test_rows_carry_numeric_sort_attributes_matching_their_scores():
    row = {"rank": 3, "audit_id": "a1", "business_name": "Peak", "city": "COS",
           "segment": "Dialed", "scores": {"found": 20, "chosen": 25, "booked": 30, "total": 75},
           "phone": "", "partial": False, "checks": {}}
    page = views.render_batch("b1", [row], {"Dialed": 1}, [], csrf="t")
    assert 'data-sort_found="20"' in page
    assert 'data-sort_chosen="25"' in page
    assert 'data-sort_booked="30"' in page
    assert 'data-sort_total="75"' in page
    assert 'data-sort_business="peak"' in page


def test_rows_without_a_score_sort_before_scored_rows_not_after():
    """A missing score must not sort as the biggest number by accident."""
    row = {"rank": 1, "audit_id": "a1", "business_name": "X", "city": "",
           "segment": None, "scores": {}, "phone": "", "partial": True, "checks": {}}
    page = views.render_batch("b1", [row], {}, [], csrf="t")
    assert 'data-sort_total="-1"' in page


def test_the_search_needle_combines_business_and_city_lowercased():
    row = {"rank": 1, "audit_id": "a1", "business_name": "Peak ROOFING", "city": "Colorado Springs",
           "segment": "Dialed", "scores": {}, "phone": "", "partial": False, "checks": {}}
    page = views.render_batch("b1", [row], {}, [], csrf="t")
    assert 'data-business="peak roofing colorado springs"' in page


def test_score_headers_are_sortable_and_explained():
    headers = views.score_headers()
    assert 'data-sort="found"' in headers
    assert 'data-sort="chosen"' in headers
    assert 'data-sort="booked"' in headers
    assert "abbr title=" in headers


def test_batch_page_has_no_forbidden_dash_with_filters_present():
    from app.copy_rules import contains_forbidden_dash

    defs = _defs({"code": "C16", "title": "Footer copyright", "section": "chosen"})
    row = {"rank": 1, "audit_id": "a1", "business_name": "Peak", "city": "COS",
           "segment": "Leaky Bucket", "scores": {"found": 1, "chosen": 2, "booked": 3, "total": 6},
           "phone": "", "partial": False, "checks": {"C16": "fail"}}
    page = views.render_batch("b1", [row], {"Leaky Bucket": 1}, defs, csrf="t")
    assert not contains_forbidden_dash(page)


# ── _assemble_batch wires check statuses onto each row ───────────────────────


def test_assemble_batch_attaches_per_audit_check_statuses(monkeypatch):
    import app.console.routes as routes

    audits = [{"audit_id": "a1", "prospect_id": "p1", "segment": "Leaky Bucket",
              "scores": {"found": 1, "chosen": 2, "booked": 3, "total": 6}}]
    monkeypatch.setattr(routes.store, "audits_for_batch", lambda b: audits)
    monkeypatch.setattr(routes.store, "get_prospect", lambda p: {"business_name": "Peak"})
    monkeypatch.setattr(routes.store, "get_draft_findings", lambda a: None)
    monkeypatch.setattr(routes.store, "audit_checks",
                        lambda a: [{"code": "C16", "status": "fail"}, {"code": "B1", "status": "pass"}])
    monkeypatch.setattr(routes.store, "all_check_defs", lambda: [
        {"code": "C16", "title": "Footer copyright", "section": "chosen",
         "enabled": True, "sort_order": 1},
        {"code": "F1", "title": "Off this week", "section": "found",
         "enabled": False, "sort_order": 2},
    ])

    rows, segments, check_defs = routes._assemble_batch("b1")
    assert rows[0]["checks"] == {"C16": "fail", "B1": "pass"}
    assert [d["code"] for d in check_defs] == ["C16"], "disabled checks are excluded"


def test_the_prospects_website_opens_in_a_new_tab():
    """An operator working the call list should not lose their place in the
    console every time they check a prospect's actual site."""
    page = views.render_audit(
        audit={"audit_id": "a1", "scores": {}, "batch_id": "b1"},
        prospect={"business_name": "Peak", "website_url": "https://peakroofing.com/",
                 "domain": "peakroofing.com"},
        checks=[], definitions={}, findings=None, evidence=[], csrf="t",
    )
    assert ('href="https://peakroofing.com/" target="_blank" '
            'rel="noopener noreferrer"') in page


# ── Google Business Profile link ─────────────────────────────────────────────


def test_the_audit_page_links_to_the_google_business_profile():
    """Places gives us googleMapsUri on every prospect. It opens the public
    profile, which is where an operator checks reviews and hours, and is what
    a searching homeowner would land on."""
    page = views.render_audit(
        audit={"audit_id": "a1", "scores": {}, "batch_id": "b1"},
        prospect={"business_name": "Peak", "maps_uri": "https://maps.google.com/?cid=1",
                 "website_url": "https://peakroofing.com/", "domain": "peakroofing.com"},
        checks=[], definitions={}, findings=None, evidence=[], csrf="t",
    )
    assert ('href="https://maps.google.com/?cid=1" target="_blank" '
            'rel="noopener noreferrer">Google Business Profile</a>') in page


def test_no_profile_link_is_rendered_when_we_have_no_uri():
    """A prospect ingested before we stored maps_uri must not get a dead link."""
    page = views.render_audit(
        audit={"audit_id": "a1", "scores": {}}, prospect={"business_name": "Peak"},
        checks=[], definitions={}, findings=None, evidence=[], csrf="t",
    )
    assert "Google Business Profile" not in page


# ── Sweeping an arbitrary city ───────────────────────────────────────────────


def test_the_market_field_accepts_any_city_and_suggests_the_mapped_metros():
    page = views.render_run(csrf="t", markets=["Colorado Springs", "Pueblo"],
                            active_jobs=[], recent_batches=[])
    assert '<select id="market"' not in page, "a dropdown would block unmapped cities"
    assert 'list="known-markets"' in page and "<datalist" in page
    assert 'value="Colorado Springs, CO"' in page
    assert 'value="Pueblo, CO"' in page


def test_the_market_field_explains_what_an_unmapped_city_changes():
    """resolve_market returns boundaries_known=False for anywhere unmapped, so
    the local-operator check reports unknown rather than failing. An operator
    seeing more prospects reach review deserves to know why."""
    page = views.render_run(csrf="t", markets=["Denver"], active_jobs=[], recent_batches=[])
    assert "not sure" in page, "an unmapped city must say the check is inconclusive"
    assert "extra companies" in page


def test_an_arbitrary_city_resolves_without_being_a_known_metro():
    from app.markets import resolve_market

    spec = resolve_market("Pueblo West, CO")
    assert spec.state == "CO"
    assert spec.boundaries_known is False
    assert spec.in_metro("Anywhere", "CO") is None, "advisory, never a blocking fail"


# ── The coordinator card explains itself ─────────────────────────────────────


def test_the_coordinator_card_says_when_to_use_it_and_when_not_to():
    import re

    page = views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[])
    # The copy wraps across source lines, so compare on normalized whitespace.
    flat = re.sub(r"\s+", " ", page)
    assert "Worth using when" in flat
    assert "Use the buttons on the left instead" in flat
    assert "cannot contact anybody" in flat, "rule 4 stated where an operator reads it"


# ── Evidence is shown, not just named ────────────────────────────────────────


def test_a_screenshot_renders_inline_and_links_to_full_size():
    """The bucket blocks public access, so a stored screenshot is only viewable
    through a signed URL minted per page load. Printing the storage path, which
    is what this page used to do, shows the operator nothing."""
    evidence = [{"kind": "screenshot", "gcs_path": "evidence/p/a/homepage.jpg",
                "size_bytes": 614923, "url": "https://storage.googleapis.com/signed?sig=abc"}]
    page = views.render_audit(
        audit={"audit_id": "a1", "scores": {}}, prospect={"business_name": "Peak"},
        checks=[], definitions={}, findings=None, evidence=evidence, csrf="t",
    )
    assert 'class="evidence-shot"' in page
    assert 'src="https://storage.googleapis.com/signed?sig=abc"' in page
    assert 'target="_blank"' in page, "click through to full size"
    assert "601 KB" in page


def test_evidence_that_could_not_be_signed_still_lists_itself():
    """A signing failure must not blank the section: the operator should still
    see what was captured, and why they cannot view it."""
    evidence = [{"kind": "screenshot", "gcs_path": "evidence/p/a/homepage.jpg",
                "size_bytes": 2048, "url_error": "RefreshError: token expired"}]
    page = views.render_audit(
        audit={"audit_id": "a1", "scores": {}}, prospect={"business_name": "Peak"},
        checks=[], definitions={}, findings=None, evidence=evidence, csrf="t",
    )
    assert "could not sign a link" in page
    assert "RefreshError" in page
    assert 'class="evidence-shot"' not in page


def test_an_audit_with_no_evidence_says_so():
    page = views.render_audit(
        audit={"audit_id": "a1", "scores": {}}, prospect={"business_name": "Peak"},
        checks=[], definitions={}, findings=None, evidence=[], csrf="t",
    )
    assert "No screenshot was saved" in page


def test_evidence_urls_are_minted_per_row(monkeypatch):
    import app.console.routes as routes

    class FakeStore:
        def audit_evidence(self, audit_id):
            return [{"kind": "screenshot", "gcs_path": "p/1.jpg", "size_bytes": 10},
                    {"kind": "screenshot", "gcs_path": "p/2.jpg", "size_bytes": 20}]

        def signed_url(self, path):
            return f"https://signed.example/{path}"

    rows = routes._evidence_with_urls(FakeStore(), "a1")
    assert [r["url"] for r in rows] == ["https://signed.example/p/1.jpg",
                                        "https://signed.example/p/2.jpg"]


def test_a_signing_failure_is_captured_per_row_not_raised(monkeypatch):
    import app.console.routes as routes

    class BrokenStore:
        def audit_evidence(self, audit_id):
            return [{"kind": "screenshot", "gcs_path": "p/1.jpg", "size_bytes": 10}]

        def signed_url(self, path):
            raise RuntimeError("no signing credentials")

    rows = routes._evidence_with_urls(BrokenStore(), "a1")
    assert "url" not in rows[0]
    assert "no signing credentials" in rows[0]["url_error"]
