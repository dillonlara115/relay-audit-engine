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
    from app.console.auth import SESSION_COOKIE, unpack

    return unpack(client.cookies.get(SESSION_COOKIE))[1]


# ── The gate ──────────────────────────────────────────────────────────────────


def test_the_console_is_closed_without_a_key(client):
    assert client.get("/console").status_code == 401
    assert client.get("/console/jobs").status_code == 401
    assert client.get("/console/batches").status_code == 401


def test_a_wrong_key_is_refused(client):
    assert client.get("/console?key=nope").status_code == 401
    assert "__session" not in client.cookies


def test_the_key_becomes_a_session_and_leaves_the_url(client):
    first = client.get(f"/console?key={SECRET}", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/console"
    assert "__session" in first.cookies

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
    from app.console.auth import LOGIN_PATH
    from app.console.routes import router

    for route in router.routes:
        path = getattr(route, "path", "")
        if not path or path == LOGIN_PATH:
            continue   # login is where the password is entered
        concrete = path.replace("{job_id}", "j1").replace("{batch_id}", "b1") \
                       .replace("{audit_id}", "a1")
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            response = client.request(method, concrete, follow_redirects=False)
            assert response.status_code == 401, f"{method} {concrete} answered "\
                                                f"{response.status_code} without a session"


# ── The rules a web app could erode ───────────────────────────────────────────


# The ledger route records a touch a human already sent from their own mailbox.
# It is the one path allowed to mention outreach, and it is spelled out here so
# that adding a second one is a deliberate edit to this list.
LEDGER_ROUTES = {"/console/outreach/{prospect_id}/log-touch"}


def test_there_is_no_send_route(client):
    """Rule 4: drafts only, no automated sending. A button is how that erodes."""
    from app.console.routes import router

    paths = " ".join(getattr(r, "path", "") for r in router.routes).lower()
    for word in ("send", "email", "message", "deliver", "campaign"):
        assert word not in paths, f"a {word} route exists in the console"


def test_the_ledger_is_the_only_route_that_touches_outreach(client):
    """A path may say 'outreach' only if it is on the list above."""
    from app.console.routes import router

    named = {getattr(r, "path", "") for r in router.routes
             if "outreach" in getattr(r, "path", "").lower()}
    assert named == LEDGER_ROUTES


def test_nothing_in_the_app_can_transmit_mail():
    """The real guard on rule 4, and a much harder one to erode than a URL.

    A send button has to import something that speaks to a mail server. None of
    these appear anywhere in the package, so no route can send whatever it is
    called.
    """
    import pathlib

    banned = ("smtplib", "sendgrid", "mailgun", "postmarker", "boto3",
              "resend", "aiosmtplib", "yagmail", "mailjet", "sparkpost")
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in root.rglob("*.py"):
        source = path.read_text()
        for name in banned:
            if f"import {name}" in source or f"from {name}" in source:
                offenders.append(f"{path.name} imports {name}")
    assert offenders == [], offenders


def test_the_ledger_route_records_rather_than_sends():
    """Its own docstring has to say so, because that is what the next person reads."""
    from app.console.routes import log_touch

    doc = (log_touch.__doc__ or "").lower()
    assert "does not send" in doc
    assert "suppression" in doc


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
        {"ordinal": i, "what_we_saw": f"saw {i}", "what_it_means": "y",
         "what_fixing_takes": "z"} for i in range(1, 7)
    ]}
    page = views.render_audit(audit=audit, prospect={"business_name": "Peak"},
                              checks=[], definitions={}, findings=findings,
                              evidence=[], csrf="t")
    assert "Choose these three for the report" in page
    assert "Tick the three the owner should read" in page
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
    monkeypatch.setattr(routes.store, "sequences_for_batch", lambda b: {})
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


# ── P0: accessible color system ───────────────────────────────────────────────
#
# Computed with the WCAG relative-luminance formula, not eyeballed. The brand
# orange (#F25C1F) is 2.67:1 on chalk and 3.32:1 on white as text, both below
# the 4.5:1 AA floor. It stays as a fill color; a darker step on the same hue
# (--ember) carries text and links instead.


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg: str, bg: str) -> float:
    la, lb = _luminance(fg), _luminance(bg)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_the_brand_orange_still_fails_as_text_this_is_the_documented_reason_it_moved():
    assert _contrast("#F25C1F", "#ECE6DC") < 4.5
    assert _contrast("#F25C1F", "#ffffff") < 4.5


def test_ember_the_replacement_text_color_passes_everywhere_it_is_used():
    ember = "#B0400E"
    assert _contrast(ember, "#ECE6DC") >= 4.5, "ember on chalk"
    assert _contrast(ember, "#ffffff") >= 4.5, "ember on a white card"


def test_button_text_passes_against_the_orange_fill():
    """Buttons keep the orange fill; the text on it is asphalt, not white."""
    assert _contrast("#16120E", "#F25C1F") >= 4.5


def test_links_render_in_ember_not_the_brand_fill_orange():
    page = views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[])
    assert "color:var(--ember)" in page
    assert "a { color:var(--orange)" not in page


def test_nothing_sets_small_text_on_a_solid_orange_fill():
    """White on the brand orange is 3.32:1, and both of these sit below the
    size that would let 3:1 count. The running pill keeps its orange fill and
    takes asphalt text. The active nav item no longer has a solid fill at all:
    it is a tint with --ember text at 5.07:1, marked by an orange edge."""
    page = views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[])
    assert ".status.running { background:var(--orange); color:var(--asphalt)" in page

    nav_on = page.split(".side nav a.on")[1].split("}")[0]
    assert "background:var(--orange)" not in nav_on, "a solid brand fill is back"
    assert "color:var(--ember)" in nav_on
    assert "border-left-color:var(--orange)" in nav_on, "the edge is what marks the page"


# ── P1.1: double submit guard ─────────────────────────────────────────────────


def test_every_console_page_carries_the_submit_guard():
    """A slow redirect must not let a second click start a second identical
    job and spend real Places or Vertex quota twice."""
    for page in (
        views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[]),
        views.render_batch("b1", [], {}, [], csrf="t"),
        views.render_jobs([]),
    ):
        assert "button.disabled = true" in page


# ── P1.3: keyboard access ─────────────────────────────────────────────────────


def test_focus_visible_styles_exist_for_links_and_buttons():
    page = views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[])
    assert "a:focus-visible" in page
    assert "button:focus-visible" in page


def test_sortable_headers_are_reachable_and_operable_by_keyboard():
    """A header a mouse can click but Tab cannot reach, and Enter cannot
    activate, does not exist for someone who does not use a mouse."""
    page = views.render_batch("b1", [], {}, [], csrf="t")
    assert "setAttribute('tabindex', '0')" in page
    assert "setAttribute('role', 'button')" in page
    assert "e.key === 'Enter'" in page and "e.key === ' '" in page


# ── P1.4: mobile table containment ────────────────────────────────────────────


def test_tables_are_wrapped_so_only_the_table_scrolls_on_a_narrow_screen():
    for page in (
        views.render_batch("b1", [], {}, [], csrf="t"),
        views.render_jobs([]),
        views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[]),
    ):
        assert '<div class="table-wrap"><table' in page


# ── P2: empty states, progress color, scan labels, sidebar texture ───────────


def test_render_jobs_content_matches_its_intent_not_just_its_title():
    """Regression: an earlier pass changed the page <title> to "Activity" and
    the nav label, but never touched the actual <h1> or lede inside the
    function, and a weak "Activity" in page assertion missed it because the
    title alone satisfied it. Pin the real content this time."""
    page = views.render_jobs([])
    assert "<h1>Jobs</h1>" in page
    assert "<h1>Activity</h1>" not in page
    assert "Params" not in page, "raw JSON must not be a column a person reads"


def test_every_empty_state_offers_the_next_step():
    results = views.render_batches([])
    assert "Start one" in results and 'href="/console"' in results

    activity = views.render_jobs([])
    assert "shows up here" in activity

    call_list = views.render_batch("b1", [], {}, [], csrf="t")
    assert "finishes on its own" in call_list
    assert 'href="/console/jobs"' in call_list


def test_progress_bar_turns_green_only_when_actually_complete():
    assert 'class="bar done"' in views.progress_bar(40, 40)
    assert 'class="bar done"' not in views.progress_bar(39, 40)
    assert 'class="bar done"' not in views.progress_bar(0, 0), "no work is not done work"


def test_scan_label_prefers_market_and_date_over_the_raw_id():
    from datetime import datetime, timezone

    labeled = views.scan_label({"batch_id": "xY9z", "market": "Colorado Springs",
                                "started_at": datetime(2026, 8, 26, tzinfo=timezone.utc)})
    assert "Colorado Springs" in labeled and "Aug 26" in labeled

    market_only = views.scan_label({"batch_id": "xY9z", "market": "Pueblo"})
    assert market_only == "Pueblo"

    # A batch built without a sweep behind it (CLI, a smoke test) has neither
    # on record. The raw id must still be there, not a blank cell.
    fallback = views.scan_label({"batch_id": "smoke-074927"})
    assert fallback == "smoke-074927"


def test_scan_label_escapes_the_market_name():
    labeled = views.scan_label({"batch_id": "b1", "market": "<script>alert(1)</script>"})
    assert "<script>" not in labeled
    assert "&lt;script&gt;" in labeled


def test_the_sidebar_is_a_light_rail_held_by_one_hairline():
    """The rail used to be a near-black bar carrying a grid texture at 5%
    chalk, which only reads on a dark surface. It is now white against the
    warm field, so the texture is gone and a single border does the work."""
    page = views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[])
    assert "repeating-linear-gradient" not in page, "texture belonged to the dark rail"
    assert "border-right:1px solid var(--line)" in page


def test_the_wordmark_does_not_lean_on_the_large_text_exemption():
    """Brand orange is 3.32:1 on white, which clears AA only by counting as
    large text. The wordmark appears on every screen, so it takes --ember."""
    page = views.render_run(csrf="t", markets=["X"], active_jobs=[], recent_batches=[])
    assert ".side .brand" in page
    brand = page.split(".side .brand")[1].split("}")[0]
    assert "var(--ember)" in brand
    assert "var(--orange)" not in brand


def test_batch_overview_enriches_rows_with_market_and_start_date(monkeypatch):
    from datetime import datetime, timezone

    import app.store.firestore as store

    tasks = [{"batch_id": "b1", "status": "done", "updated_at": store.utcnow()}]

    class FakeSnap:
        def __init__(self, data):
            self._data = data
            self.exists = data is not None

        def to_dict(self):
            return self._data

    class FakeQuery:
        def where(self, **kw):
            return self

        def stream(self):
            return [FakeSnap(t) for t in tasks]

    class FakeCollection:
        def __init__(self, name):
            self.name = name

        def where(self, **kw):
            return FakeQuery()

        def document(self, doc_id):
            return self

        def get(self):
            if self.name == "batches":
                return FakeSnap({"market_id": "colorado-springs",
                                 "created_at": datetime(2026, 8, 26, tzinfo=timezone.utc)})
            if self.name == "markets":
                return FakeSnap({"name": "Colorado Springs"})
            return FakeSnap(None)

    class FakeClient:
        def collection(self, name):
            return FakeCollection(name)

    monkeypatch.setattr(store, "get_client", lambda: FakeClient())
    rows = store.batch_overview()
    assert rows[0]["market"] == "Colorado Springs"
    assert rows[0]["started_at"].year == 2026


def test_batch_overview_falls_back_gracefully_with_no_batch_document(monkeypatch):
    """A batch the CLI or a smoke test built by hand has no batches/{id}
    document at all. The overview must not raise, just omit the label data."""
    import app.store.firestore as store

    tasks = [{"batch_id": "smoke-1", "status": "done", "updated_at": store.utcnow()}]

    class FakeSnap:
        exists = False

        def to_dict(self):
            return None

    class FakeQuery:
        def where(self, **kw):
            return self

        def stream(self):
            return [type("S", (), {"to_dict": lambda self=None, t=t: t})() for t in tasks]

    class FakeCollection:
        def where(self, **kw):
            return FakeQuery()

        def document(self, doc_id):
            return self

        def get(self):
            return FakeSnap()

    class FakeClient:
        def collection(self, name):
            return FakeCollection()

    monkeypatch.setattr(store, "get_client", lambda: FakeClient())
    rows = store.batch_overview()
    assert rows[0]["market"] is None
    assert rows[0]["started_at"] is None


# ── findings that predate their audit ─────────────────────────────────────────
#
# An audit document is keyed by prospect and batch, so re-checking a site
# overwrites its results in place while the findings keep text drafted against
# the older ones. A contractor who fixed the very thing we named would still
# read it named.


def _dt(day):
    from datetime import datetime, timezone
    return datetime(2026, 9, day, tzinfo=timezone.utc)


def test_findings_drafted_before_the_latest_check_are_flagged():
    from app.console.views import findings_predate_audit

    assert findings_predate_audit({"drafted_at": _dt(1)}, {"started_at": _dt(5)})


def test_findings_drafted_after_the_check_are_current():
    from app.console.views import findings_predate_audit

    assert not findings_predate_audit({"drafted_at": _dt(5)}, {"started_at": _dt(1)})


def test_a_missing_timestamp_never_raises_a_false_alarm():
    from app.console.views import findings_predate_audit

    assert not findings_predate_audit({}, {"started_at": _dt(5)})
    assert not findings_predate_audit({"drafted_at": _dt(1)}, {})
    assert not findings_predate_audit(None, {"started_at": _dt(5)})


def test_mixed_naive_and_aware_timestamps_do_not_raise():
    """Firestore hands back tz-aware values; a fixture or an older document may
    not. Comparing them raises, and a crash here would take out the whole audit
    screen for a warning."""
    from datetime import datetime

    from app.console.views import findings_predate_audit

    assert not findings_predate_audit(
        {"drafted_at": datetime(2026, 9, 1)}, {"started_at": _dt(5)})


def test_the_stale_warning_reaches_the_screen():
    page = views.render_audit(
        audit={"audit_id": "a1", "scores": {}, "started_at": _dt(5)},
        prospect={"business_name": "Peak"}, checks=[], definitions={},
        findings={"status": "approved", "drafted_at": _dt(1),
                  "findings": [{"ordinal": 1, "what_we_saw": "x",
                                "what_it_means": "y", "what_fixing_takes": "z"}]},
        evidence=[], csrf="t",
    )
    assert "checked again after these were written" in page


# ── The outreach ledger in the console ────────────────────────────────────────


def test_a_prospect_with_no_address_is_not_styled_as_a_failure():
    """No address on the site is a thing to go and find, not a red mark."""
    cell = views.contact_cell([])
    assert "none on the site" in cell
    assert "tag bad" not in cell


def test_the_contact_cell_leads_with_the_best_address_and_counts_the_rest():
    cell = views.contact_cell([
        {"email": "dave@whitakerroofing.com", "status": "valid"},
        {"email": "info@whitakerroofing.com", "status": "risky"},
    ])
    assert "dave@whitakerroofing.com" in cell
    assert "+1 more" in cell
    assert "info@whitakerroofing.com" not in cell


def test_an_undeliverable_address_is_not_offered_at_all():
    assert "x@dead.com" not in views.contact_cell([{"email": "x@dead.com", "status": "invalid"}])


def test_an_unverified_address_is_still_offered_with_a_caveat():
    """Unknown means we did not check, not that it is dead."""
    cell = views.contact_cell([{"email": "dave@x.com", "status": "unknown"}])
    assert "dave@x.com" in cell
    assert "unchecked" in cell


def test_the_contact_cell_names_no_mechanism():
    """Copy rule: outcome language. 'Role address' and 'MX' are mechanisms."""
    cells = [views.contact_cell([{"email": "a@b.com", "status": s}])
             for s in ("valid", "risky", "unknown")]
    flat = " ".join(cells).lower()
    for word in ("mx", "role address", "dns", "smtp", "catch-all"):
        assert word not in flat


def test_a_touch_cannot_be_logged_before_a_report_exists():
    cell = views.outreach_cell(None, prospect_id="p1", audit_id="a1", csrf="t",
                               can_start=False)
    assert "log-touch" not in cell


def test_the_ledger_cell_shows_the_position_in_the_sequence():
    from app import outreach

    seq = outreach.advance(outreach.open_sequence("p1"))
    cell = views.outreach_cell(seq.to_dict(), prospect_id="p1", audit_id="a1",
                               csrf="t", can_start=True)
    assert "1 of 4 sent" in cell


def test_a_finished_sequence_offers_no_further_touch():
    from app import outreach

    seq = outreach.apply_policy(outreach.advance(outreach.open_sequence("p1")),
                                outreach.NOT_INTERESTED,
                                outreach.ReplyPolicy(close=True, suppress=True))
    cell = views.outreach_cell(seq.to_dict(), prospect_id="p1", audit_id="a1",
                               csrf="t", can_start=True)
    assert "log-touch" not in cell
    assert "Not interested" in cell


def test_the_ledger_copy_carries_no_em_dash():
    from app import outreach

    seq = outreach.advance(outreach.open_sequence("p1"))
    blob = "".join([
        views.contact_cell([{"email": "a@b.com", "status": "risky"}]),
        views.contact_cell([]),
        views.outreach_cell(seq.to_dict(), prospect_id="p1", audit_id="a1",
                            csrf="t", can_start=True),
    ])
    assert not contains_forbidden_dash(blob)


def _ledger_store(monkeypatch, *, suppressions=None, sequence=None, pool=6):
    import app.console.routes as routes

    written: dict = {"touches": [], "sequences": []}
    monkeypatch.setattr(routes.store, "get_draft_findings", lambda aid: {
        "findings": [{"ordinal": i} for i in range(1, pool + 1)],
        "selected": [1, 2, 3],
    })
    monkeypatch.setattr(routes.store, "get_prospect",
                        lambda pid: {"domain": "whitakerroofing.com",
                                     "owner_email": "dave@whitakerroofing.com"})
    monkeypatch.setattr(routes.store, "load_suppressions",
                        lambda: suppressions or {"place_id": set(), "domain": set(),
                                                 "phone": set(), "email": set()})
    monkeypatch.setattr(routes.store, "get_sequence", lambda pid: sequence)
    monkeypatch.setattr(routes.store, "add_touch",
                        lambda pid, touch: written["touches"].append((pid, touch)) or "t1")
    monkeypatch.setattr(routes.store, "save_sequence",
                        lambda seq: written["sequences"].append(seq))
    return written


def test_logging_a_touch_advances_the_sequence(client, monkeypatch):
    written = _ledger_store(monkeypatch)
    csrf = sign_in(client)

    response = client.post("/console/outreach/p1/log-touch",
                           data={"csrf": csrf, "audit_id": "a1"}, follow_redirects=False)

    assert response.status_code == 303
    assert written["touches"][0][1]["ordinal"] == 1
    assert written["sequences"][0].touch_count == 1
    assert written["sequences"][0].next_due_at is not None


def test_a_suppressed_prospect_cannot_have_a_touch_logged(client, monkeypatch):
    """Rule 3: suppression is checked before every outreach action."""
    written = _ledger_store(monkeypatch, suppressions={
        "place_id": set(), "phone": set(), "email": set(),
        "domain": {"whitakerroofing.com"},
    })
    csrf = sign_in(client)

    response = client.post("/console/outreach/p1/log-touch",
                           data={"csrf": csrf, "audit_id": "a1"})

    assert "suppressed" in response.text.lower()
    assert written["touches"] == []
    assert written["sequences"] == []


def test_logging_a_touch_needs_a_csrf_token(client, monkeypatch):
    written = _ledger_store(monkeypatch)
    sign_in(client)

    response = client.post("/console/outreach/p1/log-touch", data={"csrf": "wrong"})

    assert response.status_code == 403
    assert written["touches"] == []


def test_a_finished_sequence_records_nothing_further(client, monkeypatch):
    from app import outreach

    spent = outreach.Sequence(prospect_id="p1", status=outreach.CLOSED, touch_count=4)
    written = _ledger_store(monkeypatch, sequence=spent.to_dict())
    csrf = sign_in(client)

    response = client.post("/console/outreach/p1/log-touch", data={"csrf": csrf})

    assert "finished" in response.text.lower()
    assert written["touches"] == []


# ── Choosing three from the pool ──────────────────────────────────────────────


def _audit_page(findings):
    return views.render_audit(
        audit={"audit_id": "a1", "scores": {}, "batch_id": "b1"},
        prospect={"business_name": "Peak"}, checks=[], definitions={},
        findings=findings, evidence=[], csrf="t",
    )


def _pool(n=6, **doc):
    return {"status": "draft", "needs_review": False,
            "findings": [{"ordinal": i, "what_we_saw": f"saw {i}",
                          "what_it_means": "y", "what_fixing_takes": "z"}
                         for i in range(1, n + 1)], **doc}


def test_the_whole_pool_is_shown_for_the_human_to_choose_from():
    page = _audit_page(_pool())
    for i in range(1, 7):
        assert f"saw {i}" in page
    assert page.count('name="selected"') == 6


def test_the_models_ranking_is_pre_ticked_but_only_the_top_three():
    """Rule 7 is the person changing it, so the default cannot be all six."""
    page = _audit_page(_pool())
    assert page.count("checked>") == 3


def test_an_approved_pool_shows_which_three_the_contractor_reads():
    page = _audit_page(_pool(status="approved", selected=[2, 4, 1]))
    assert "report, number 1" in page
    assert "follow up 1" in page
    assert 'name="selected"' not in page


def test_a_thin_pool_says_the_company_gets_fewer_messages():
    """Four findings is two touches, and the screen should say so plainly."""
    page = _audit_page(_pool(4, status="approved", selected=[1, 2, 3]))
    assert "2 emails rather than four" in page


def test_a_full_pool_does_not_apologise_for_itself():
    assert "rather than four" not in _audit_page(_pool(6, status="approved",
                                                       selected=[1, 2, 3]))


def test_the_selection_copy_carries_no_em_dash():
    assert not contains_forbidden_dash(_audit_page(_pool()))
    assert not contains_forbidden_dash(_audit_page(_pool(status="approved",
                                                         selected=[1, 2, 3])))


def test_approving_records_the_three_a_person_picked(client, monkeypatch):
    import app.console.routes as routes

    got: dict = {}
    monkeypatch.setattr(routes.store, "approve_report_findings",
                        lambda aid, sel, **kw: got.update(audit=aid, selected=sel))
    csrf = sign_in(client)

    response = client.post("/console/audits/a1/approve",
                           data={"csrf": csrf, "selected": ["2", "4", "1"]},
                           follow_redirects=False)

    assert response.status_code == 303
    assert got["selected"] == [2, 4, 1]


def test_approving_the_wrong_number_is_refused_with_a_reason(client, monkeypatch):
    """The refusal rides the redirect as a notice; the page it lands on shows it."""
    import app.console.routes as routes
    from urllib.parse import unquote_plus

    def boom(aid, sel, **kw):
        raise ValueError("a report carries exactly 3 findings, got 2")

    monkeypatch.setattr(routes.store, "approve_report_findings", boom)
    csrf = sign_in(client)

    response = client.post("/console/audits/a1/approve",
                           data={"csrf": csrf, "selected": ["1", "2"]},
                           follow_redirects=False)

    assert response.status_code == 303
    location = unquote_plus(response.headers["location"])
    assert location.startswith("/console/audits/a1?")
    assert "notice=not_approved" in location
    assert "exactly 3 findings" in location


def test_a_thin_pool_closes_the_sequence_early(client, monkeypatch):
    """Four findings buys two touches, not four."""
    written = _ledger_store(monkeypatch, pool=4)
    csrf = sign_in(client)

    client.post("/console/outreach/p1/log-touch",
                data={"csrf": csrf, "audit_id": "a1"}, follow_redirects=False)

    assert written["sequences"][0].max_touches == 2
    assert written["sequences"][0].next_due_at is not None


# ── Closed by default, open by exception ──────────────────────────────────────


def test_a_route_nobody_thought_about_is_private(client):
    """The point of the inversion. A path not on the open list needs a session
    without anyone having remembered to guard it."""
    from app.worker import is_open_path

    for path in ("/export", "/admin", "/api/prospects", "/", "/dashboardish"):
        assert is_open_path(path) is False
        assert client.get(path, follow_redirects=False).status_code == 401


def test_the_open_list_is_the_whole_public_surface(client):
    """If this list grows, it should be because somebody decided to grow it."""
    from app.worker import OPEN_PREFIXES

    assert set(OPEN_PREFIXES) == {
        "/r/", "/console/login", "/health", "/healthz",
        "/robots.txt", "/pubsub/", "/tick",
    }


def test_a_report_answers_without_a_password(client, monkeypatch):
    """A contractor cannot log in, so this one path stays open on purpose."""
    monkeypatch.setattr("app.report.publish.render_by_slug", lambda slug: "<html>report</html>")
    monkeypatch.setattr("app.report.publish.log_view", lambda *a, **k: None)

    response = client.get("/abcdefghijklmnop")

    assert response.status_code == 200
    assert "report" in response.text


def test_a_report_that_does_not_exist_is_a_404_not_a_login(client, monkeypatch):
    """A wrong slug must not leak that there is a console behind this."""
    monkeypatch.setattr("app.report.publish.render_by_slug", lambda slug: None)
    assert client.get("/abcdefghijklmnop").status_code == 404


def test_health_answers_for_cloud_run(client):
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_robots_is_public_and_refuses_everything(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /" in response.text
    assert "User-agent: *" in response.text


def test_every_response_carries_noindex_however_it_ended(client, monkeypatch):
    """Including the ones nobody wrote a header for: 401s, 404s, robots itself."""
    monkeypatch.setattr("app.report.publish.render_by_slug", lambda slug: None)

    for path in ("/health", "/robots.txt", "/console", "/export",
                 "/abcdefghijklmnop", "/nothing-here"):
        response = client.get(path, follow_redirects=False)
        assert response.headers.get("X-Robots-Tag") == "noindex, nofollow", path


def test_the_gated_401_itself_is_not_indexable(client):
    response = client.get("/console", follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_a_signed_in_operator_still_reaches_the_console(client):
    """The inversion must not have locked out the person who has the password."""
    sign_in(client)
    assert client.get("/console").status_code == 200


# ── The report at the root ────────────────────────────────────────────────────


def test_a_slug_shaped_path_is_the_only_thing_open_at_the_root():
    from app.worker import is_open_path

    assert is_open_path("/" + "a" * 16) is True
    for closed in ("/" + "a" * 15, "/" + "a" * 17, "/export", "/admin",
                   "/prospects", "/", "/a.b"):
        assert is_open_path(closed) is False, closed


def test_the_open_shape_matches_what_new_slug_actually_produces():
    """If new_slug ever changes length or alphabet, this catches it before a
    published report stops resolving."""
    from app.report.data import new_slug
    from app.worker import REPORT_SLUG

    for _ in range(50):
        assert REPORT_SLUG.match("/" + new_slug())


def test_no_named_route_is_shadowed_by_the_report():
    """The report is a single-segment catch-all. Any named single-segment route
    declared after it would become unreachable, and any route whose path
    happened to be sixteen characters would become public."""
    from app.worker import REPORT_SLUG, app

    paths = [getattr(r, "path", "") for r in app.routes]
    catch_all = paths.index("/{slug}")

    for i, path in enumerate(paths):
        if not path or path == "/{slug}":
            continue
        assert not REPORT_SLUG.match(path), f"{path} is shaped like a report slug"
        single_segment = path.count("/") == 1 and "{" not in path
        assert not (single_segment and i > catch_all), \
            f"{path} is declared after the catch-all and can never be reached"


def test_an_old_report_link_still_resolves(client, monkeypatch):
    """Three were published before the move. A link already sent cannot be
    recalled."""
    monkeypatch.setattr("app.report.publish.render_by_slug", lambda slug: "<html>ok</html>")
    monkeypatch.setattr("app.report.publish.log_view", lambda *a, **k: None)

    response = client.get("/r/S8_n4NYrBhlviJJW", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "/S8_n4NYrBhlviJJW"


def test_publish_hands_back_the_new_path():
    from app.report.publish import PublishResult

    assert PublishResult(slug="abcdefghijklmnop", audit_id="a1",
                         url_path="/abcdefghijklmnop").url_path.startswith("/")


def test_a_junk_slug_on_the_old_path_is_not_an_open_redirect():
    from app.worker import REPORT_SLUG

    for hostile in ("//evil.com", "..%2f..%2fadmin", "a" * 40):
        assert not REPORT_SLUG.match(f"/{hostile}")


# ── The console does not exist on the contractor's hostname ───────────────────


PUBLIC_HOST = "reports.relayforroofers.com"


@pytest.fixture()
def public_client(monkeypatch):
    """A client whose requests look like they arrived via Firebase Hosting."""
    import app.console.auth as auth
    import app.console.routes as routes
    import app.worker as worker

    monkeypatch.setattr(auth, "get_config", lambda: Config(console_password=SECRET))
    monkeypatch.setattr(worker, "get_config",
                        lambda: Config(console_password=SECRET,
                                       public_report_host=PUBLIC_HOST))
    monkeypatch.setattr(routes, "publish_job", lambda *a, **k: "msg-1")
    monkeypatch.setattr(routes.jobs, "active", lambda: [])
    monkeypatch.setattr(routes.jobs, "recent", lambda n=40: [])
    monkeypatch.setattr(routes.store, "batch_overview", lambda days=14: [])

    from app.worker import app

    return TestClient(app, raise_server_exceptions=False,
                      headers={"X-Forwarded-Host": PUBLIC_HOST})


def test_the_console_does_not_exist_on_the_public_hostname(public_client):
    """A contractor who trims the slug off the URL must not find a login box."""
    for path in ("/console", "/dashboard", "/export", "/admin"):
        response = public_client.get(path, follow_redirects=False)
        assert response.status_code == 404, path
        assert "password" not in response.text.lower()


def test_the_report_still_answers_on_the_public_hostname(public_client, monkeypatch):
    monkeypatch.setattr("app.report.publish.render_by_slug", lambda slug: "<html>ok</html>")
    monkeypatch.setattr("app.report.publish.log_view", lambda *a, **k: None)
    assert public_client.get("/abcdefghijklmnop").status_code == 200


def test_the_operator_entrance_still_answers_with_a_login(client):
    """The Cloud Run URL is not in the public host set, so it is unchanged."""
    assert client.get("/console", follow_redirects=False).status_code == 401


def test_the_host_header_is_used_when_there_is_no_proxy(monkeypatch):
    import app.console.auth as auth
    import app.worker as worker

    monkeypatch.setattr(auth, "get_config", lambda: Config(console_password=SECRET))
    monkeypatch.setattr(worker, "get_config",
                        lambda: Config(console_password=SECRET,
                                       public_report_host=PUBLIC_HOST))
    from app.worker import app

    direct = TestClient(app, raise_server_exceptions=False,
                        headers={"Host": f"{PUBLIC_HOST}:443"})
    assert direct.get("/console", follow_redirects=False).status_code == 404


def test_an_unset_public_host_changes_nothing(client):
    """Inert until the domain is actually pointed here."""
    from app.worker import public_hosts

    assert public_hosts() == frozenset()
    assert client.get("/console", follow_redirects=False).status_code == 401


def test_spoofing_the_header_only_ever_tells_an_attacker_less(public_client):
    """Both headers are caller controlled. The only thing a forged one buys is
    a 404 where a 401 would have been, which is strictly less information."""
    forged = public_client.get("/console", headers={"X-Forwarded-Host": PUBLIC_HOST},
                               follow_redirects=False)
    assert forged.status_code == 404


@pytest.mark.parametrize("raw", [
    "reports.relayforroofers.com,report.example.com:8080",
    "reports.relayforroofers.com report.example.com:8080",   # gcloud-safe, no comma
    "reports.relayforroofers.com; report.example.com:8080",
    "  reports.relayforroofers.com ,  report.example.com:8080  ",
])
def test_several_hostnames_can_be_listed_however_they_are_separated(monkeypatch, raw):
    """gcloud splits --update-env-vars on commas, so a space separated list has
    to work or every deploy needs the ^delimiter^ escape."""
    import app.worker as worker

    monkeypatch.setattr(worker, "get_config", lambda: Config(public_report_host=raw))
    assert worker.public_hosts() == {"reports.relayforroofers.com", "report.example.com"}


def test_the_bare_hostname_gives_nothing_away(public_client):
    """Trimming a report URL down to / lands on the same 404 as any other
    non-report path. It used to 302 to the marketing site, which told a visitor
    whose domain this was before they had any business knowing."""
    response = public_client.get("/", follow_redirects=False)

    assert response.status_code == 404
    assert "location" not in {k.lower() for k in response.headers}


def test_the_root_is_not_an_open_path():
    from app.worker import is_open_path

    assert is_open_path("/") is False


# ── The password prompt ───────────────────────────────────────────────────────


def test_a_logged_out_visitor_gets_a_form_not_a_bare_401(client):
    """No login URL to remember: the prompt is at the page you asked for."""
    response = client.get("/console", follow_redirects=False)

    assert response.status_code == 401
    assert 'type="password"' in response.text
    assert 'action="/console/login"' in response.text


def test_the_prompt_does_not_trigger_the_browsers_own_dialog(client):
    """A WWW-Authenticate header would replace the page with a basic-auth box."""
    response = client.get("/console", follow_redirects=False)
    assert "www-authenticate" not in {k.lower() for k in response.headers}


def test_the_prompt_says_nothing_about_what_is_behind_it(client):
    """A trimmed report URL lands here too, so the page describes nothing.

    The word "console" survives in the form's own action and that is fine: a
    path segment says nothing about what the tool does or who it is for. What
    must not appear is the wordmark, the tagline, the nav, or any of the
    explanatory comments in the stylesheet.
    """
    flat = client.get("/console", follow_redirects=False).text.lower()
    for leak in ("audit", "prospect", "roofer", "call list", "dashboard",
                 "find roofers", "leaky bucket", "segment"):
        assert leak not in flat, leak


def test_the_prompt_is_not_indexable(client):
    response = client.get("/console", follow_redirects=False)
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert "no-store" in response.headers.get("Cache-Control", "")


def test_the_right_password_starts_a_session(client):
    response = client.post("/console/login", data={"password": SECRET, "next": "/console"},
                           follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/console"
    # One cookie, because Firebase Hosting forwards exactly one.
    from app.console.auth import unpack

    assert "__session" in response.cookies
    session, csrf = unpack(response.cookies["__session"])
    assert session and csrf


def test_signing_in_lands_on_the_page_you_were_going_to(client):
    response = client.post("/console/login",
                           data={"password": SECRET, "next": "/console/batches"},
                           follow_redirects=False)
    assert response.headers["location"] == "/console/batches"


def test_the_form_carries_the_page_you_were_going_to(client):
    page = client.get("/console/batches", follow_redirects=False).text
    assert 'name="next" value="/console/batches"' in page


def test_a_wrong_password_returns_the_form_with_a_message(client):
    response = client.post("/console/login", data={"password": "nope", "next": "/console"},
                           follow_redirects=False)

    assert response.status_code == 401
    assert "not right" in response.text
    assert "__session" not in response.cookies


def test_an_empty_password_is_not_a_way_in(client):
    for attempt in ("", "   "):
        response = client.post("/console/login", data={"password": attempt},
                               follow_redirects=False)
        assert response.status_code == 401


def test_login_cannot_be_turned_into_an_open_redirect(client):
    """A login page that honours an arbitrary next= is how a phish gets built."""
    from app.console.auth import safe_next

    for hostile in ("//evil.com", "https://evil.com", "///evil.com",
                    "/\\evil.com", "\\\\evil.com", None, ""):
        assert safe_next(hostile) == "/console"

    response = client.post("/console/login",
                           data={"password": SECRET, "next": "//evil.com"},
                           follow_redirects=False)
    assert response.headers["location"] == "/console"


def test_next_never_points_back_at_the_form(client):
    from app.console.auth import safe_next

    assert safe_next("/console/login") == "/console"


def test_the_key_in_the_url_still_works_for_a_bookmark(client):
    response = client.get(f"/console?key={SECRET}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/console"


def test_a_wrong_key_in_the_url_shows_the_form(client):
    response = client.get("/console?key=nope", follow_redirects=False)
    assert response.status_code == 401
    assert 'type="password"' in response.text


def test_a_failed_attempt_is_logged_without_a_raw_ip(client, caplog):
    """Guardrail 5. Seeing one source hammer the form must not mean storing it."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING):
        client.post("/console/login", data={"password": "nope"},
                    headers={"X-Forwarded-For": "203.0.113.9"})

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "203.0.113.9" not in logged
    assert "login failed" in logged


def test_the_login_copy_carries_no_em_dash():
    assert not contains_forbidden_dash(views.render_login())


def test_the_session_rides_in_the_one_cookie_hosting_forwards():
    """Firebase Hosting strips every cookie except __session on its way to
    Cloud Run. A pair of nicely named cookies works against the Cloud Run URL
    and is silently dropped through Hosting, which looks like a login form that
    takes the right password and then asks again."""
    from app.console.auth import SESSION_COOKIE

    assert SESSION_COOKIE == "__session"


def test_the_two_values_survive_the_round_trip():
    from app.console.auth import pack, unpack

    session, csrf = unpack(pack("a" * 64, "tok-en_123"))
    assert (session, csrf) == ("a" * 64, "tok-en_123")


@pytest.mark.parametrize("raw", [None, "", "no-dot-here", ".", "...", "onlysession."])
def test_a_malformed_cookie_signs_nobody_in(raw, monkeypatch):
    from app.console import auth

    monkeypatch.setattr(auth, "get_config", lambda: Config(console_password=SECRET))

    class FakeRequest:
        cookies = {"__session": raw} if raw is not None else {}

    assert auth.signed_in(FakeRequest()) is False


def test_one_cookie_is_set_not_two(client):
    response = client.post("/console/login", data={"password": SECRET},
                           follow_redirects=False)
    set_cookies = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
    assert len(set_cookies) == 1
    assert set_cookies[0].startswith("__session=")


def test_the_bare_domain_takes_a_signed_in_operator_to_the_console(client):
    """Typing reports.relayforroofers.com with no path is the normal way in."""
    sign_in(client)
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/console"


def test_the_bare_domain_still_asks_a_stranger_for_the_password(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 401
    assert 'type="password"' in response.text


def test_signing_in_from_the_bare_domain_lands_on_the_console(client):
    """It used to hand back next=/ and 404 there, which is what a user saw."""
    from app.console.auth import safe_next

    assert safe_next("/") == "/console"

    page = client.get("/", follow_redirects=False).text
    assert 'name="next" value="/console"' in page

    response = client.post("/console/login", data={"password": SECRET, "next": "/"},
                           follow_redirects=False)
    assert response.headers["location"] == "/console"


# ── Which page was judged ─────────────────────────────────────────────────────


def _audit_screen(**audit):
    return views.render_audit(
        audit={"audit_id": "a1", "scores": {}, "batch_id": "b1", **audit},
        prospect={"business_name": "Red Diamond"}, checks=[], definitions={},
        findings=None, evidence=[], csrf="t",
    )


def test_a_deep_landing_page_is_named_on_the_audit_screen():
    """Google advertises /service-areas/fort-collins-roofer/ for this prospect,
    so that is the page scored. Saying so beats letting a reader assume the
    front page was judged."""
    page = _audit_screen(landing_url="https://reddiamondroof.com/service-areas/fort-collins-roofer/")

    assert "/service-areas/fort-collins-roofer/" in page
    assert "not the front page" in page


def test_a_normal_homepage_needs_no_explanation():
    page = _audit_screen(landing_url="https://reddiamondroof.com/")
    assert "not the front page" not in page


def test_an_audit_from_before_this_was_recorded_still_renders():
    assert "not the front page" not in _audit_screen()


def test_the_landing_note_carries_no_em_dash():
    page = _audit_screen(landing_url="https://x.com/locations/denver/")
    assert not contains_forbidden_dash(page)


# ── The templated shell: overview, login, sign out ────────────────────────────


def _overview(**kw):
    base = dict(csrf="t", markets=["Colorado Springs"], active_jobs=[], recent_batches=[])
    base.update(kw)
    return views.render_run(**base)


def test_the_jobs_badge_shows_how_many_are_running():
    job = {"job_id": "j1", "label": "Sweep Pueblo", "status": "running", "log": []}
    page = _overview(active_jobs=[job, job, job])
    assert 'class="badge">3<' in page


def test_no_badge_when_nothing_is_running():
    assert 'class="badge"' not in _overview()


def test_the_overview_speaks_the_new_vocabulary():
    page = _overview()
    for present in ("Run sweep", "Recent sweeps", "Run coordinator", "Overview"):
        assert present in page, present
    for gone in ("Start a scan", "Recent scans", "Find companies", "Happening right now",
                 "Results", "Activity</a>"):
        assert gone not in page, gone


def test_the_login_shell_carries_no_nav_and_no_sprite():
    """The page a stranger reaches must not list the screens behind it."""
    page = views.render_login()
    assert "<nav" not in page
    assert "<symbol" not in page
    assert "/console/logout" not in page


def test_every_nav_icon_exists_in_the_sprite():
    from pathlib import Path

    sprite = (Path(views.__file__).parent / "templates" / "_icons.svg").read_text()
    page = _overview()
    for _group, items in views.NAV_GROUPS:
        for _href, _key, _label, icon_name in items:
            assert f'id="i-{icon_name}"' in sprite, icon_name
            assert f'href="#i-{icon_name}"' in page, icon_name


def test_a_hostile_market_name_is_escaped_exactly_once():
    batch = {"batch_id": "b1", "market": "<script>alert(1)</script>",
             "total": 1, "done": 0, "running": 0, "pending": 1, "failed": 0, "latest": ""}
    page = _overview(recent_batches=[batch])
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page
    assert "&amp;lt;" not in page, "helper output was escaped twice"


def test_the_sign_out_form_needs_a_session_token_to_render():
    assert "/console/logout" in _overview()
    assert "/console/logout" not in views.shell("t", "<p>x</p>")


def test_signing_out_clears_the_session(client):
    csrf = sign_in(client)
    assert client.get("/console", follow_redirects=False).status_code == 200

    response = client.post("/console/logout", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/console"
    set_cookie = response.headers.get("set-cookie", "")
    assert "__session=" in set_cookie and ("max-age=0" in set_cookie.lower()
                                            or "expires=" in set_cookie.lower())
    assert client.get("/console", follow_redirects=False).status_code == 401


def test_signing_out_needs_csrf(client):
    """A page somebody else controls must not be able to log the operator out."""
    sign_in(client)
    assert client.post("/console/logout", data={"csrf": "wrong"}).status_code == 403
    assert client.get("/console", follow_redirects=False).status_code == 200


def test_legacy_shell_screens_keep_their_tables_and_tags():
    """Helpers hand templates Markup but Python callers plain str, because
    str + Markup escapes the str. This is the dashboard failure, pinned."""
    from app.console.views import tiles

    body = "<h2>Before</h2>" + tiles([("scans", 1)]) + "<table><tr><td>x</td></tr></table>"
    page = views.shell("t", body)
    assert "<h2>Before</h2>" in page
    assert '<div class="table-wrap"><table' in page


# ── Notices on redirect, and where an action sends you back ───────────────────


def test_a_notice_renders_its_headline_and_detail():
    notice = views.notice_from("not_approved", "a report carries exactly 3 findings, got 2")
    page = views.render_batches([], notice=notice)
    assert "Findings not approved." in page
    assert "exactly 3 findings" in page


def test_an_unknown_notice_code_renders_nothing():
    assert views.notice_from("made_up", "anything") is None
    assert views.notice_from(None, None) is None


def test_a_notice_detail_is_escaped_and_capped():
    notice = views.notice_from("not_recorded", "<img src=x onerror=alert(1)>" + "y" * 500)
    page = views.render_batches([], notice=notice)
    assert "<img src=x" not in page
    assert "&lt;img" in page
    assert len(notice[1]) == views.NOTICE_DETAIL_CAP


def test_the_notice_reaches_a_legacy_shell_screen():
    page = views.shell("t", "<p>x</p>", notice=("Report not published.", "no evidence"))
    assert "Report not published." in page and "no evidence" in page


def test_a_blocked_touch_lands_where_you_were_with_a_notice(client, monkeypatch):
    _ledger_store(monkeypatch, suppressions={
        "place_id": set(), "phone": set(), "email": set(), "domain": {"whitakerroofing.com"},
    })
    csrf = sign_in(client)

    response = client.post("/console/outreach/p1/log-touch", data={"csrf": csrf},
                           headers={"Referer": "http://testserver/console/batches/b1?tab=all"},
                           follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/console/batches/b1?")
    assert "tab=all" in location and "notice=not_recorded" in location


def test_a_referer_on_another_host_is_never_followed(client, monkeypatch):
    """An open redirect on an action route is how a convincing phish gets built."""
    _ledger_store(monkeypatch)
    csrf = sign_in(client)

    response = client.post("/console/outreach/p1/log-touch", data={"csrf": csrf},
                           headers={"Referer": "https://evil.example/console/batches/b1"},
                           follow_redirects=False)

    assert response.headers["location"] == "/console/batches"


def test_a_stale_notice_on_the_referer_is_not_carried_forward(client, monkeypatch):
    _ledger_store(monkeypatch)
    csrf = sign_in(client)

    response = client.post("/console/outreach/p1/log-touch", data={"csrf": csrf},
                           headers={"Referer": "http://testserver/console/batches/b1?notice=not_recorded&detail=old"},
                           follow_redirects=False)

    assert response.headers["location"] == "/console/batches/b1"


# ── Sweeps, Jobs, Job detail on templates ─────────────────────────────────────


def _sweep(**kw):
    base = {"batch_id": "b1", "market": "Pueblo", "total": 4, "done": 4,
            "running": 0, "pending": 0, "failed": 0, "latest": "Sep 17 09:00"}
    base.update(kw)
    return base


def test_the_sweeps_page_speaks_the_new_vocabulary():
    page = views.render_batches([_sweep()])
    for present in ("<h1>Sweeps</h1>", "Open sweep", "Run sweep", "Pueblo"):
        assert present in page, present
    for gone in ("<h1>Results</h1>", "Recent scans", "Open a scan"):
        assert gone not in page, gone


@pytest.mark.parametrize("counts,label", [
    (dict(total=4, done=4), "Finished"),
    (dict(total=4, done=2, failed=2), "Failed"),
    (dict(total=4, done=1, running=1, pending=2), "Running"),
    (dict(total=0, done=0), "Queued"),
])
def test_a_sweep_gets_one_status_pill(counts, label):
    assert label in views.render_batches([_sweep(**counts)])


def test_the_sweeps_empty_state_still_points_at_running_one():
    page = views.render_batches([])
    assert "Start one" in page and 'href="/console"' in page


def test_the_jobs_page_names_each_kind():
    from datetime import datetime, timezone

    rows = [{"job_id": "j1", "label": "Sweep Pueblo", "kind": "sweep", "status": "done",
             "created_at": datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)},
            {"job_id": "j2", "label": "Coordinator run", "kind": "agent", "status": "running"}]
    page = views.render_jobs(rows)
    assert ">Sweep<" in page and ">Coordinator<" in page
    assert "Sep 17 09:00" in page


def _job(**kw):
    base = {"job_id": "j1", "kind": "sweep", "label": "Sweep Pueblo", "status": "done",
            "result": {"batch_id": "b1", "eligible": 12}, "params": {"market": "Pueblo"},
            "log": [{"line": "ingested 40"}]}
    base.update(kw)
    return base


def test_a_finished_sweep_offers_to_audit_the_passed_prospects():
    page = views.render_job(_job(), csrf="t")
    assert "Audit passed prospects" in page
    assert 'name="batch_id" value="b1"' in page
    assert 'name="market" value="Pueblo"' in page
    assert "Dispatch audits" not in page and "survivors" not in page


def test_a_finished_non_sweep_job_links_to_its_call_list():
    page = views.render_job(_job(kind="dispatch"), csrf="t")
    assert "Open call list" in page and 'href="/console/batches/b1"' in page


def test_the_job_page_carries_its_id_for_the_poller():
    page = views.render_job(_job(status="running"), csrf="t")
    assert 'data-job="j1"' in page
    assert 'id="job-status"' in page and 'id="job-log"' in page
    assert "/console/jobs/' + id + '.json" in page


def test_a_finished_job_does_not_poll():
    assert "/console/jobs/' + id + '.json" not in views.render_job(_job(), csrf="t")


def test_a_failed_job_says_so():
    page = views.render_job(_job(status="failed", error="Places quota", result={}), csrf="t")
    assert "<strong>Failed.</strong> Places quota" in page


def test_the_ported_screens_carry_no_forbidden_dash():
    for page in (views.render_batches([_sweep()]), views.render_jobs([]),
                 views.render_job(_job(status="running"), csrf="t"),
                 views.render_job(_job(status="failed", error="x", result={}), csrf="t")):
        assert not contains_forbidden_dash(page)


# ── The dashboard merged into the console ─────────────────────────────────────


def test_the_old_dashboard_paths_redirect_permanently(client):
    """A bookmark keeps working."""
    sign_in(client)
    top = client.get("/dashboard", follow_redirects=False)
    assert (top.status_code, top.headers["location"]) == (301, "/console")
    one = client.get("/dashboard/b1", follow_redirects=False)
    assert (one.status_code, one.headers["location"]) == (301, "/console/batches/b1")


def test_the_old_dashboard_paths_stay_gated(client):
    """A stranger gets the password form, not a redirect that names a path."""
    assert client.get("/dashboard", follow_redirects=False).status_code == 401
    assert client.get("/dashboard/b1", follow_redirects=False).status_code == 401


def test_the_dashboard_module_is_gone():
    import importlib.util

    assert importlib.util.find_spec("app.report.dashboard") is None


def test_a_call_list_row_with_every_tag_carries_no_forbidden_dash():
    """The old dashboard dash test, re-homed on the call list it duplicated."""
    row = {"rank": 1, "audit_id": "a1", "business_name": "Peak <script>", "city": "COS",
           "segment": "Leaky Bucket", "scores": {"found": 1, "chosen": 2, "booked": 3, "total": 6},
           "phone": "x", "partial": True, "incumbent_agency": "scorpion", "checks": {}}
    page = views.render_batch("b1", [row], {"Leaky Bucket": 1}, csrf="t")
    assert not contains_forbidden_dash(page)
    assert "Peak <script>" not in page and "&lt;script&gt;" in page


# ── The call list on its template ─────────────────────────────────────────────


def _row(i=1, segment="Leaky Bucket", **kw):
    base = {"rank": i, "audit_id": f"a{i}", "prospect_id": f"p{i}",
            "business_name": f"Roofer {i}", "city": "COS", "segment": segment,
            "scores": {"found": 20, "chosen": 20, "booked": 10, "total": 50},
            "phone": "(719) 555-0100", "checks": {"C16": "fail"}, "contacts": [],
            "sequence": None, "report_slug": None, "findings_status": None}
    base.update(kw)
    return base


def _list(rows, **kw):
    segments = {}
    for r in rows:
        segments[r.get("segment") or "incomplete"] = segments.get(r.get("segment") or "incomplete", 0) + 1
    return views.render_batch("b1", rows, segments, csrf="t", **kw)


def test_the_call_list_speaks_the_new_vocabulary():
    page = _list([_row()])
    for present in ("<h1>Call list</h1>", "Draft findings", "Segment", "Prospect", "Re-audit"):
        assert present in page, present
    for gone in ("Who to call", "Opportunity", "Write talking points", "I sent",
                 "Next step", "all scans", "review draft"):
        assert gone not in page, gone


def test_tabs_carry_counts_and_the_active_one_is_marked():
    page = _list([_row(1), _row(2, segment="Dialed"), _row(3, segment=None)], tab="dialed")
    assert 'href="?tab=all">All <b>3</b>' in page
    assert 'href="?tab=leaky-bucket">Leaky Bucket <b>1</b>' in page
    assert 'href="?tab=incomplete">Incomplete <b>1</b>' in page
    assert 'class="on" href="?tab=dialed"' in page


def test_a_segment_tab_shows_only_its_rows():
    page = _list([_row(1), _row(2, segment="Dialed")], tab="dialed")
    assert "Roofer 2" in page and "Roofer 1" not in page


def test_an_unknown_tab_falls_back_to_all():
    page = _list([_row(1), _row(2, segment="Dialed")], tab="nonsense")
    assert "Roofer 1" in page and "Roofer 2" in page


def test_the_excluded_tab_is_not_drawn_until_it_can_be_counted():
    assert "Excluded" not in _list([_row()])


def test_each_row_offers_open_and_reaudit_and_report_only_when_published():
    page = _list([_row(1, business_name="Peak's \"Best\" Roofing", report_slug="abcdefghijklmnop")])
    assert 'action="/console/audits/a1/reaudit"' in page
    assert "Re-audit Peak" in page and "return confirm(" in page
    assert 'href="/abcdefghijklmnop" target="_blank" rel="noopener noreferrer"' in page
    assert "Publish report" not in page


def test_an_approved_row_offers_publish():
    page = _list([_row(findings_status="approved")])
    assert 'action="/console/audits/a1/publish"' in page and "Publish report" in page


@pytest.mark.parametrize("row,label", [
    (dict(), "Not drafted"),
    (dict(findings_status="draft"), "Draft"),
    (dict(findings_status="approved"), "Approved"),
    (dict(findings_status="approved", report_slug="abcdefghijklmnop"), "Published"),
])
def test_the_findings_pill_names_the_stage(row, label):
    assert f">{label}<" in _list([_row(**row)])


def test_tags_read_as_words():
    page = _list([_row(partial=True, incumbent_agency="scorpion")])
    assert ">Agency<" in page and ">Partial<" in page
    assert ">agency<" not in page and ">partial<" not in page


def test_the_segment_chip_reads_incomplete_as_a_word():
    assert ">Incomplete<" in views.chip(None)
    assert 'data-segment="incomplete"' in _list([_row(segment=None)])


def test_the_progress_banner_counts_audits():
    page = _list([_row()], progress={"total": 4, "done": 2})
    assert "2 of 4 audits finished." in page
    assert "All audits finished." in _list([_row()], progress={"total": 4, "done": 4})


def test_the_outreach_cell_is_a_pill_that_links_to_the_prospect_page():
    from app import outreach

    seq = outreach.advance(outreach.open_sequence("p1"))
    cell = views.outreach_cell(seq.to_dict(), prospect_id="p1", audit_id="a1", csrf="t",
                               can_start=True)
    assert 'href="/console/audits/a1#outreach"' in cell
    assert "<form" not in cell and "log-touch" not in cell
    assert "1 of 4 sent" in cell


def test_a_published_prospect_with_nothing_sent_is_due_its_first_email():
    cell = views.outreach_cell(None, prospect_id="p1", audit_id="a1", csrf="t", can_start=True)
    assert "Due: first email" in cell


def test_a_parked_sequence_says_why_in_the_pill():
    from app import outreach

    seq, _ = outreach.record_reply(outreach.advance(outreach.open_sequence("p1")),
                                   outreach.WRONG_PERSON)
    cell = views.outreach_cell(seq.to_dict(), prospect_id="p1", audit_id="a1", csrf="t",
                               can_start=True)
    assert "Waiting: needs a new contact" in cell


def test_the_reaudit_confirm_survives_quotes_in_a_name():
    attr = views._reaudit_confirm('Peak "Best" Roofing')
    assert "&quot;Re-audit Peak" in attr
    assert "\\&quot;Best\\&quot;" in attr


# ── calllist: the pure filters ────────────────────────────────────────────────


def test_filter_rows_by_tab_search_and_check():
    from app.console import calllist

    rows = [_row(1), _row(2, segment="Dialed", checks={"C16": "pass"}),
            _row(3, business_name="Summit Roofing", city="Pueblo")]
    assert [r["rank"] for r in calllist.filter_rows(rows, tab="dialed")] == [2]
    assert [r["rank"] for r in calllist.filter_rows(rows, q="pueblo")] == [3]
    assert [r["rank"] for r in calllist.filter_rows(rows, check="C16", status="fail")] == [1, 3]
    assert [r["rank"] for r in calllist.filter_rows(rows, check="C16")] == [1, 2, 3]
    assert calllist.filter_rows(rows, check="B1") == []


def test_tab_counts_add_up_and_leave_excluded_out_until_counted():
    from app.console import calllist

    counts = calllist.tab_counts({"Leaky Bucket": 2, "Dialed": 1, "incomplete": 3})
    assert counts["all"] == 6 and counts["leaky-bucket"] == 2 and counts["incomplete"] == 3
    assert "excluded" not in counts
    assert calllist.tab_counts({}, excluded=4)["excluded"] == 4


# ── The prospect page and the Outreach card ───────────────────────────────────


def _prospect_page(*, findings=None, audit=None, prospect=None, **kw):
    a = {"audit_id": "a1", "scores": {"found": 20, "chosen": 20, "booked": 10, "total": 50},
         "batch_id": "b1", "prospect_id": "p1"}
    a.update(audit or {})
    pr = {"business_name": "Apex Roofing", "city": "Denver", "place_id": "p1"}
    pr.update(prospect or {})
    return views.render_audit(audit=a, prospect=pr, checks=[], definitions={},
                              findings=findings, evidence=[], csrf="t", **kw)


def _approved(n=6):
    return _pool(n, status="approved", selected=[1, 2, 3])


def test_the_prospect_page_speaks_the_new_vocabulary():
    page = _prospect_page(findings=_approved(), audit={"report_slug": "abcdefghijklmnop"})
    for present in ("Re-audit", "Suppress prospect", "Open report", "Findings", "Outreach", "Evidence"):
        assert present in page, present
    for gone in ("Check this site again", "Never contact<", "Talking points", "What we saw",
                 "shareable report", " he ", " him "):
        assert gone not in page, gone


def test_compose_opens_the_mail_client_with_the_report_and_the_three_findings():
    page = _prospect_page(findings=_approved(), audit={"report_slug": "abcdefghijklmnop"},
                          prospect={"owner_email": "dave@apexroofingusa.com"},
                          report_url="https://reports.relayforroofers.com/abcdefghijklmnop")
    assert 'href="mailto:dave@apexroofingusa.com?subject=' in page
    assert "body=" in page
    assert "reports.relayforroofers.com%2Fabcdefghijklmnop" in page
    assert "Nothing is sent from here" in page
    assert "for dave@apexroofingusa.com" in page


def test_without_an_address_compose_still_opens_but_says_so():
    page = _prospect_page(findings=_approved(), audit={"report_slug": "abcdefghijklmnop"},
                          report_url="https://x/abc")
    assert 'href="mailto:?subject=' in page
    assert "No address on record" in page
    assert "paste it into the To field" in page


def test_without_a_published_report_there_is_nothing_to_compose_or_mark():
    page = _prospect_page(findings=_approved())
    assert "mailto:" not in page
    assert "Publish the report first" in page
    assert "log-touch" not in page


def test_mark_as_sent_confirms_the_recipient_and_that_nothing_is_sent(client, monkeypatch):
    page = _prospect_page(findings=_approved(), audit={"report_slug": "abcdefghijklmnop"},
                          prospect={"owner_email": "dave@apexroofingusa.com"},
                          report_url="https://x/abc")
    assert 'action="/console/outreach/p1/log-touch"' in page
    assert "Mark as sent" in page
    assert "Mark email 1 of 4 to dave@apexroofingusa.com as sent?" in page
    assert "Nothing is sent from here." in page
    assert 'name="audit_id" value="a1"' in page


def test_the_timeline_lists_sends_and_replies_and_the_next_finding():
    from datetime import datetime, timezone

    from app import outreach

    seq = outreach.advance(outreach.open_sequence("p1"),
                           sent_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    page = _prospect_page(
        findings=_approved(), audit={"report_slug": "abcdefghijklmnop"},
        prospect={"owner_email": "dave@apexroofingusa.com"}, report_url="https://x/abc",
        sequence=seq.to_dict(),
        touches=[{"ordinal": 1, "sent_at": datetime(2026, 9, 17, tzinfo=timezone.utc)}],
        replies=[{"received_at": datetime(2026, 9, 18, tzinfo=timezone.utc),
                  "intent": "interested", "from_email": "dave@apexroofingusa.com",
                  "excerpt": "Sure, call me Thursday."}],
    )
    assert "Email 1 sent Sep 17" in page
    assert "Reply Sep 18: Interested (dave@apexroofingusa.com)" in page
    assert "Next due Sep 20, carrying follow-up finding 1: saw 4" in page
    assert "Mark email 2 of 4" in page
    assert "follow-up finding 1: saw 4" in page


def test_a_closed_sequence_shows_why_and_offers_nothing():
    from app import outreach

    seq, _ = outreach.record_reply(outreach.advance(outreach.open_sequence("p1")),
                                   outreach.NOT_INTERESTED)
    page = _prospect_page(findings=_approved(), audit={"report_slug": "abcdefghijklmnop"},
                          report_url="https://x/abc", sequence=seq.to_dict())
    assert "Closed: Not interested" in page
    assert "mailto:" not in page and "log-touch" not in page


def test_a_parked_sequence_points_at_the_cli():
    from app import outreach

    seq, _ = outreach.record_reply(outreach.advance(outreach.open_sequence("p1")),
                                   outreach.WRONG_PERSON)
    page = _prospect_page(findings=_approved(), audit={"report_slug": "abcdefghijklmnop"},
                          report_url="https://x/abc", sequence=seq.to_dict())
    assert "Waiting: needs a new contact" in page
    assert "python -m app.cli replies" in page


def test_the_suppress_confirm_keeps_its_warning_and_survives_a_quote():
    page = _prospect_page(prospect={"business_name": "Pete's Roofing"})
    assert "return confirm(" in page and "cannot be undone" in page
    assert "Never contact Pete" in page


def test_check_results_read_as_words_with_their_classes():
    checks = [{"code": "C16", "status": "fail", "points_awarded": 0, "note": "old year"},
              {"code": "B1", "status": "skipped", "points_awarded": 0, "note": ""}]
    defs = {"C16": {"section": "chosen", "title": "Footer copyright", "points": 1, "sort_order": 1},
            "B1": {"section": "booked", "title": "Self-serve booking", "points": 10, "sort_order": 2}}
    page = views.render_audit(audit={"audit_id": "a1", "scores": {}, "batch_id": "b1"},
                              prospect={"business_name": "X"}, checks=checks, definitions=defs,
                              findings=None, evidence=[], csrf="t")
    assert '<td class="fail">Fail</td>' in page
    assert '<td class="skip">Skipped</td>' in page
    assert "<h2>Chosen</h2>" in page and "<h2>Booked</h2>" in page
    assert "<h2>Found</h2>" not in page


def test_the_partial_banner_names_the_sections():
    page = _prospect_page(audit={"partial_sections": ["found", "booked"]})
    assert "Partial audit." in page and "found, booked sections" in page
    assert "left as Incomplete" in page


def test_the_prospect_page_carries_no_forbidden_dash():
    from app import outreach

    seq = outreach.advance(outreach.open_sequence("p1"))
    for page in (
        _prospect_page(),
        _prospect_page(findings=_pool()),
        _prospect_page(findings=_approved(), audit={"report_slug": "abcdefghijklmnop"},
                       prospect={"owner_email": "d@x.com"}, report_url="https://x/abc",
                       sequence=seq.to_dict()),
    ):
        assert not contains_forbidden_dash(page)


def test_a_touch_logged_from_the_prospect_page_lands_back_on_the_card(client, monkeypatch):
    _ledger_store(monkeypatch)
    csrf = sign_in(client)
    response = client.post("/console/outreach/p1/log-touch", data={"csrf": csrf, "audit_id": "a1"},
                           headers={"Referer": "http://testserver/console/audits/a1"},
                           follow_redirects=False)
    assert response.headers["location"] == "/console/audits/a1#outreach"


def test_soft_reads_degrade_and_log_instead_of_raising(caplog):
    import logging as _logging

    import app.console.routes as routes

    def boom():
        raise RuntimeError("firestore hiccup")

    with caplog.at_level(_logging.WARNING):
        assert routes._soft(boom, "fallback") == "fallback"
    assert "firestore hiccup" in caplog.text
