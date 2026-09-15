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
    assert "Use these three" in page
    assert "Tick the three he should read" in page
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


def test_the_dashboard_tables_are_wrapped_too():
    """Applied once in shell(), so both console and dashboard inherit it."""
    from app.report import dashboard

    assert '<div class="table-wrap"><table' in dashboard.render_overview([])
    assert '<div class="table-wrap"><table' in dashboard.render_batch("b1", [], {})


# ── P2: empty states, progress color, scan labels, sidebar texture ───────────


def test_render_jobs_content_matches_its_intent_not_just_its_title():
    """Regression: an earlier pass changed the page <title> to "Activity" and
    the nav label, but never touched the actual <h1> or lede inside the
    function, and a weak "Activity" in page assertion missed it because the
    title alone satisfied it. Pin the real content this time."""
    page = views.render_jobs([])
    assert "<h1>Activity</h1>" in page
    assert "<h1>Jobs</h1>" not in page
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
    assert "2 messages rather than four" in page or "2 message rather than four" in page


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
    import app.console.routes as routes

    def boom(aid, sel, **kw):
        raise ValueError("a report carries exactly 3 findings, got 2")

    monkeypatch.setattr(routes.store, "approve_report_findings", boom)
    csrf = sign_in(client)

    response = client.post("/console/audits/a1/approve",
                           data={"csrf": csrf, "selected": ["1", "2"]})

    assert "exactly 3 findings" in response.text


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
        "/r/", "/health", "/healthz", "/robots.txt", "/pubsub/", "/tick",
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
