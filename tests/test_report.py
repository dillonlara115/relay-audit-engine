"""The report layer. Every enforced rule from engine spec section 8.

The em-dash scan, the three-findings assertion, the no-scores payload walk and
the publish gate are the rules CLAUDE.md marks as build-failing, so they live
here rather than in review comments.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.copy_rules import EM_DASH, contains_forbidden_dash
from app.report.data import (
    FORBIDDEN_PAYLOAD_TERMS,
    PublicFinding,
    PublicReport,
    hash_ip,
    new_slug,
)
from app.report.template import render_report


def finding(i: int, **overrides) -> PublicFinding:
    base = dict(
        ordinal=i,
        what_we_saw="Nobody can book a time without waiting for a call back.",
        what_it_means="Homeowners who want an answer now call whoever gives one.",
        what_fixing_takes="A service the office can turn on in an afternoon.",
    )
    base.update(overrides)
    return PublicFinding(**base)


def report(**overrides) -> PublicReport:
    base = dict(
        slug=new_slug(),
        business_name="Peak Roofing & Sons",
        city="Colorado Springs",
        findings=(finding(1), finding(2), finding(3)),
    )
    base.update(overrides)
    return PublicReport(**base)


# ── Exactly three ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_a_report_refuses_anything_but_three_findings(count):
    with pytest.raises(ValueError, match="exactly 3"):
        report(findings=tuple(finding(i + 1) for i in range(count)))


# ── No scores in the public payload ───────────────────────────────────────────


def test_the_public_payload_carries_no_scores_bands_or_segments():
    from app.report.data import forbidden_terms_in

    assert forbidden_terms_in(json.dumps(report().to_dict())) == []


def test_the_rendered_page_carries_no_scores_bands_or_segments():
    from app.report.data import forbidden_terms_in

    assert forbidden_terms_in(render_report(report())) == []


def test_the_term_scan_is_word_bounded():
    """Found on the first real published page: 'band' inside 'abandoned'.

    A homeowner abandoning a slow site is exactly the language findings should
    use, so the internal-vocabulary scan matches words, not substrings."""
    from app.report.data import forbidden_terms_in

    assert forbidden_terms_in("leads are abandoned at the finish line") == []
    assert forbidden_terms_in("scores a partially good result") == []
    assert forbidden_terms_in("his Booked score drops") == ["score"]
    assert forbidden_terms_in("the Leaky Bucket segment") == ["segment", "leaky"]


# ── The em-dash build test ────────────────────────────────────────────────────


def test_no_forbidden_dash_in_any_report_source_string():
    """CLAUDE.md: a test fails the build if an em-dash appears in report
    templates. This scans the template source itself."""
    for module in ("app/report/template.py", "app/report/data.py", "app/report/publish.py"):
        assert not contains_forbidden_dash(Path(module).read_text()), module


def test_no_forbidden_dash_in_a_rendered_page():
    assert not contains_forbidden_dash(render_report(report()))


def test_findings_with_a_dash_still_cannot_reach_a_clean_page():
    """Defence in depth: the diagnostician sanitizes, the publish gate rechecks
    the rendered page. If both ever fail, this documents the second gate."""
    dirty = report(findings=(
        finding(1, what_it_means=f"Jobs {EM_DASH} gone."), finding(2), finding(3)))
    assert contains_forbidden_dash(render_report(dirty)), \
        "the dash survives rendering, which is why publish() re-scans the page"


# ── Structure and escaping ────────────────────────────────────────────────────


def test_the_page_holds_the_required_structure():
    page = render_report(report())
    assert 'name="robots" content="noindex,nofollow"' in page
    assert "What we did" in page
    assert "Three things costing you booked jobs" in page
    assert "We cannot see how fast your team actually moves" in page, \
        "the honest Booked framing is not optional"
    assert "lead-leakage-calculator" in page
    assert "reply to the message" in page.lower()
    assert "#16120E".lower() in page.lower() and "#F25C1F".lower() in page.lower()
    assert "Barlow Condensed" in page and "Work Sans" in page


def test_untrusted_text_is_escaped():
    page = render_report(report(business_name='Peak <script>alert(1)</script>'))
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_the_screenshot_block_only_renders_when_evidence_exists():
    with_shot = render_report(report(screenshot_url="https://storage.example/x.jpg"))
    without = render_report(report(screenshot_url=None))
    assert "What we found" in with_shot and "img" in with_shot
    assert "What we found" not in without


def test_sixteen_px_minimum_holds():
    assert "font-size: 16px" in render_report(report())


# ── Slug and IP hashing ───────────────────────────────────────────────────────


def test_slugs_are_sixteen_chars_and_unique():
    slugs = {new_slug() for _ in range(200)}
    assert len(slugs) == 200
    assert all(len(s) == 16 for s in slugs)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{16}", s) for s in slugs)


def test_ip_hashing_never_yields_the_raw_ip():
    hashed = hash_ip("203.0.113.9", salt="pepper")
    assert "203.0.113.9" not in hashed
    assert hashed == hash_ip("203.0.113.9", salt="pepper"), "stable for counting"
    assert hashed != hash_ip("203.0.113.9", salt="other"), "salt matters"


def test_hashing_without_a_salt_refuses():
    with pytest.raises(ValueError, match="REPORT_IP_SALT"):
        hash_ip("203.0.113.9", salt="")


# ── The publish gate ──────────────────────────────────────────────────────────


def test_publish_blocks_unapproved_findings(monkeypatch):
    """Rule 7 at the last gate: drafts do not ship."""
    from app.report import publish as pub

    monkeypatch.setattr(pub.store, "get_audit", lambda a: {"prospect_id": "p1"})
    monkeypatch.setattr(pub.store, "get_prospect", lambda p: {"business_name": "X"})
    monkeypatch.setattr(pub.store, "load_suppressions", lambda: {})
    monkeypatch.setattr(pub.store, "suppression_hit", lambda *a, **k: None)
    monkeypatch.setattr(pub.store, "get_draft_findings",
                        lambda a: {"status": "draft", "findings": []})
    with pytest.raises(pub.PublishBlocked, match="not approved"):
        pub.publish("a1")


def test_publish_blocks_without_evidence(monkeypatch):
    """Guardrail 10: no stored evidence, no claims."""
    from app.report import publish as pub

    monkeypatch.setattr(pub.store, "get_audit", lambda a: {"prospect_id": "p1"})
    monkeypatch.setattr(pub.store, "get_prospect", lambda p: {"business_name": "X"})
    monkeypatch.setattr(pub.store, "load_suppressions", lambda: {})
    monkeypatch.setattr(pub.store, "suppression_hit", lambda *a, **k: None)
    monkeypatch.setattr(pub.store, "get_draft_findings",
                        lambda a: {"status": "approved", "findings": []})
    monkeypatch.setattr(pub.evidence_store, "audit_evidence", lambda a: [])
    with pytest.raises(pub.PublishBlocked, match="evidence"):
        pub.publish("a1")


def test_publish_blocks_a_suppressed_prospect(monkeypatch):
    """Rule 3, checked at the very last moment before anything ships."""
    from app.report import publish as pub

    monkeypatch.setattr(pub.store, "get_audit", lambda a: {"prospect_id": "p1"})
    monkeypatch.setattr(pub.store, "get_prospect", lambda p: {"business_name": "X"})
    monkeypatch.setattr(pub.store, "load_suppressions", lambda: {"place_id": {"p1"}})
    with pytest.raises(pub.PublishBlocked, match="suppressed"):
        pub.publish("a1")


# ── The dashboard gate ────────────────────────────────────────────────────────


@pytest.fixture()
def dash_client(monkeypatch):
    """The dashboard shares its gate with the console (app.console.auth), so
    that is what has to be patched, not app.worker: patching the wrong module
    silently leaves the real .env secret in force and every test still passes
    by accident until the real secret does not match "dash-secret"."""
    from fastapi.testclient import TestClient

    from app.config import Config
    from app.worker import app as worker_app

    monkeypatch.setattr("app.console.auth.get_config",
                        lambda: Config(console_password="dash-secret"))
    return TestClient(worker_app, raise_server_exceptions=False)


def test_the_dashboard_is_gated(dash_client):
    assert dash_client.get("/dashboard").status_code == 401
    assert dash_client.get("/dashboard?key=wrong").status_code == 401


def test_the_key_becomes_a_cookie_and_leaves_the_url(dash_client, monkeypatch):
    monkeypatch.setattr("app.worker._batch_overview", lambda: [])
    first = dash_client.get("/dashboard?key=dash-secret", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/dashboard"
    assert "relay_console" in first.cookies  # shared with /console

    page = dash_client.get("/dashboard")  # cookie persisted by the client
    assert page.status_code == 200
    assert "dash-secret" not in page.text, "the secret never appears in a page"
    assert "Overview" in page.text
    assert 'class="side"' in page.text, "the dashboard shares the console sidebar"


def test_the_dashboard_never_renders_the_secret(dash_client, monkeypatch):
    monkeypatch.setattr("app.worker._batch_overview", lambda: [])
    dash_client.get("/dashboard?key=dash-secret", follow_redirects=False)
    page = dash_client.get("/dashboard")
    assert "dash-secret" not in page.text


def test_dashboard_pages_are_noindex(dash_client, monkeypatch):
    monkeypatch.setattr("app.worker._batch_overview", lambda: [])
    dash_client.get("/dashboard?key=dash-secret", follow_redirects=False)
    page = dash_client.get("/dashboard")
    assert page.headers["x-robots-tag"] == "noindex, nofollow"


def test_dashboard_templates_carry_no_forbidden_dash():
    from app.report.dashboard import render_batch, render_overview

    overview = render_overview([{"batch_id": "b1", "total": 4, "done": 2,
                                 "running": 1, "pending": 1, "failed": 0,
                                 "latest": "today"}])
    batch = render_batch("b1", [{"rank": 1, "business_name": "Peak <script>",
                                 "city": "COS", "segment": "Leaky Bucket",
                                 "scores": {"found": 1, "chosen": 2, "booked": 3,
                                            "total": 6}, "phone": "x",
                                 "partial": True, "incumbent_agency": "scorpion"}],
                         {"Leaky Bucket": 1})
    assert not contains_forbidden_dash(overview)
    assert not contains_forbidden_dash(batch)
    # The page legitimately carries a <script> tag now (the submit guard), so
    # the real assertion is that the business name specifically was escaped,
    # not that the substring never appears anywhere on the page.
    assert "Peak <script>" not in batch, "the raw name must not appear unescaped"
    assert "&lt;script&gt;" in batch


def test_the_dashboard_and_console_share_one_shell():
    """They drifted apart once already: the dashboard kept its own copy of the
    shell and the session gate, so it stayed on the old centred layout and a
    different cookie for two days. One layout, defined in one place."""
    from app.console.views import SEGMENT_COLORS as console_palette
    from app.report import dashboard

    assert dashboard.SEGMENT_COLORS is console_palette

    overview = dashboard.render_overview([])
    batch = dashboard.render_batch("b1", [], {})
    for page in (overview, batch):
        assert 'class="side"' in page, "sidebar present"
        assert "Start a scan" in page, "nav links back to the console"
    # read only: the dashboard renders no way to change anything
    for page in (overview, batch):
        assert "<form" not in page
        assert "<button" not in page
