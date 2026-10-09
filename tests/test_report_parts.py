"""Findings a person writes or rewords, and the optional report sections."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app import findings_edit as fe
from app.copy_rules import contains_forbidden_dash
from app.report import extras
from app.report.data import forbidden_terms_in
from app.report.template import render_report
from tests.test_console import _prospect_page, client, sign_in  # noqa: F401
from tests.test_report import report

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
GOOD = {"what_we_saw": "The phone number at the top of the site is an old one.",
        "what_it_means": "Callers reach a number nobody answers and try the next roofer.",
        "what_fixing_takes": "A five minute change to the site header."}


def _doc(**over):
    return {"status": "draft", "drafted_at": T0, "findings": [
        {"code": c, "ordinal": i, "what_we_saw": f"saw {i}", "what_it_means": "m", "what_fixing_takes": "f"}
        for i, c in enumerate(("B1", "C5", "F3", "B4"), start=1)], **over}


# ── Writing and rewording ─────────────────────────────────────────────────────


def test_a_custom_finding_joins_the_pool_with_its_own_code():
    pool = fe.add(_doc(), GOOD)
    row = pool[-1]
    assert row["code"] == "X1" and row["ordinal"] == 5 and row["custom"] is True
    assert row["what_we_saw"] == GOOD["what_we_saw"]
    assert fe.add({"findings": pool}, GOOD)[-1]["code"] == "X2"


def test_the_copy_rules_hold_for_a_person_too():
    assert "—" not in fe.add(_doc(), {**GOOD, "what_it_means": "Callers give up — and call someone else."})[-1]["what_it_means"]
    with pytest.raises(fe.FindingRejected, match="score"):
        fe.add(_doc(), {**GOOD, "what_we_saw": "Your score is low."})
    with pytest.raises(fe.FindingRejected, match="All three"):
        fe.add(_doc(), {**GOOD, "what_fixing_takes": "  "})
    flagged = fe.add(_doc(), {**GOOD, "what_we_saw": "There is no schema on the page."})[-1]
    assert flagged["mechanism_flags"] == ["schema"]


def test_rewording_any_finding_marks_it_edited():
    pool = fe.edit(_doc(), 2, GOOD)
    row = next(f for f in pool if f["ordinal"] == 2)
    assert row["what_we_saw"] == GOOD["what_we_saw"] and row["edited"] is True and row["code"] == "C5"


def test_only_a_custom_finding_off_the_report_can_be_removed():
    pool = fe.add(_doc(), GOOD)
    with pytest.raises(fe.FindingRejected, match="Only a finding you wrote"):
        fe.remove({"findings": pool}, 1)
    with pytest.raises(fe.FindingRejected, match="on the report"):
        fe.remove({"findings": pool, "status": "approved", "selected": [5, 1, 2]}, 5)
    assert [f["ordinal"] for f in fe.remove({"findings": pool}, 5)] == [1, 2, 3, 4]


def test_a_fresh_draft_keeps_what_a_person_wrote():
    old = {"findings": fe.add(_doc(), GOOD)}
    drafted = [{"code": "B1", "ordinal": 1}, {"code": "F3", "ordinal": 2}, {"code": "C5", "ordinal": 3}]
    pool = fe.carry_custom(old, drafted)
    assert [(f["code"], f["ordinal"]) for f in pool] == [("B1", 1), ("F3", 2), ("C5", 3), ("X1", 4)]


# ── The console ───────────────────────────────────────────────────────────────


@pytest.fixture()
def pool_store(client, monkeypatch):
    import app.console.routes as routes

    env = {"doc": _doc(), "saved": [], "audit": {"prospect_id": "p1"}}
    monkeypatch.setattr(routes.store, "get_audit", lambda aid: env["audit"])
    monkeypatch.setattr(routes.store, "get_draft_findings", lambda aid: env["doc"])
    monkeypatch.setattr(routes.store, "set_findings_pool", lambda aid, pool: env["saved"].append(pool))
    monkeypatch.setattr(routes.store, "update_audit", lambda aid, f: env["saved"].append(f))
    return env


def test_adding_a_finding_from_the_console(client, pool_store):
    csrf = sign_in(client)
    r = client.post("/console/audits/a1/findings/custom", data={"csrf": csrf, **GOOD}, follow_redirects=False)
    assert "notice=finding_added" in r.headers["location"] and r.headers["location"].endswith("#findings")
    assert pool_store["saved"][0][-1]["custom"] is True


def test_a_rejected_finding_says_why_and_saves_nothing(client, pool_store):
    csrf = sign_in(client)
    r = client.post("/console/audits/a1/findings/custom",
                    data={"csrf": csrf, **GOOD, "what_we_saw": "Low score."}, follow_redirects=False)
    assert "notice=finding_rejected" in r.headers["location"] and not pool_store["saved"]


def test_editing_and_removing_need_a_fresh_form(client, pool_store):
    sign_in(client)
    assert client.post("/console/audits/a1/findings/2/edit", data={"csrf": "x", **GOOD}).status_code == 403
    assert client.post("/console/audits/a1/findings/2/remove", data={"csrf": "x"}).status_code == 403


def test_every_card_can_be_reworded_and_a_custom_one_removed():
    doc = {**_doc(), "findings": fe.add(_doc(), GOOD)}
    page = _prospect_page(findings=doc)
    assert page.count("Edit wording") == 5 and 'action="/console/audits/a1/findings/3/edit"' in page
    assert page.count("/remove\"") == 1 and "Written by you" in page
    assert 'action="/console/audits/a1/findings/custom"' in page


def test_with_nothing_drafted_a_person_can_still_write_one():
    page = _prospect_page()
    assert '<details class="fedit add-own" open>' in page


# ── Report sections ───────────────────────────────────────────────────────────

LH = {"scores": {"performance": 34, "accessibility": 81, "best_practices": 92, "seo": 85},
      "metrics": {"lcp_ms": 6840, "lcp_source": "field", "speed_index_ms": 5100},
      "psi_url": "https://pagespeed.web.dev/report?url=x", "measured_at": T0}
TECH = {"status": "done", "pages_crawled": 46, "health": 78, "issues": [
    {"key": "broken_links", "label": "Broken links", "count": 7},
    {"key": "no_title", "label": "Pages with no title", "count": 1},
    {"key": "no_favicon", "label": "Pages with no favicon", "count": 46},
    {"key": "low_content_rate", "label": "Thin pages", "count": 40}]}


def test_snapshots_speak_the_owners_language():
    speed = extras.speed_snapshot(LH)
    assert [r["label"] for r in speed["ratings"]] == ["Speed", "Accessibility", "Best practices", "Search basics"]
    assert speed["main_content"] == "6.8 seconds" and speed["real_visitors"] and speed["measured"] == "October 1, 2026"
    check = extras.site_check_snapshot(TECH)
    assert check["lines"] == ["7 links lead nowhere", "1 page has no title for Google to show",
                              "40 pages have very little text on them"]
    assert "favicon" not in json.dumps(check)
    assert "very little text" not in json.dumps(extras.site_check_snapshot(TECH, text_unreliable=True))
    assert extras.site_check_snapshot({"status": "done", "pages_crawled": 0}) is None


def test_the_report_carries_the_sections_and_still_passes_its_rules():
    r = report(speed=extras.speed_snapshot(LH), site_check=extras.site_check_snapshot(TECH))
    page = render_report(r)
    assert "Google's own speed test" in page and 'style="--v: 34"' in page and "Search basics" in page
    assert "6.8 seconds to show up for real visitors on phones" in page
    assert 'href="https://pagespeed.web.dev/report?url=x"' in page
    assert "Every page, checked" in page and "7 links lead nowhere" in page and "78 out of 100" in page
    assert forbidden_terms_in(page) == [] and not contains_forbidden_dash(page)
    assert forbidden_terms_in(json.dumps(r.to_dict())) == []


def test_without_sections_the_report_is_unchanged():
    page = render_report(report())
    assert "Google's own speed test" not in page and "Every page, checked" not in page


def test_a_clean_crawl_says_so():
    page = render_report(report(site_check={"pages": 12, "health": 97, "lines": []}))
    assert "Nothing broken turned up" in page


def test_saving_sections_takes_a_snapshot(client, pool_store):
    pool_store["audit"] = {"prospect_id": "p1", "lighthouse": LH, "technical": TECH}
    csrf = sign_in(client)
    r = client.post("/console/audits/a1/report-sections", data={"csrf": csrf, "speed": "1"}, follow_redirects=False)
    assert "notice=sections_saved" in r.headers["location"]
    saved = pool_store["saved"][-1]["report_extras"]
    assert saved["speed"]["ratings"][0]["value"] == 34 and saved["site_check"] is None


def test_asking_for_a_section_with_no_results_says_so(client, pool_store):
    csrf = sign_in(client)
    r = client.post("/console/audits/a1/report-sections", data={"csrf": csrf, "site_check": "1"},
                    follow_redirects=False)
    assert "notice=sections_partial" in r.headers["location"]


def test_the_panel_shows_the_screenshot_and_what_is_switched_on():
    page = _prospect_page(audit={"lighthouse": LH, "technical": TECH,
                                 "report_extras": {"speed": extras.speed_snapshot(LH), "updated_at": T0}})
    assert 'id="on-report"' in page and "Change screenshot" in page
    assert 'name="speed" value="1" checked' in page and "Saved Oct 01" in page
    assert 'name="site_check" value="1">' in page, "ready, not switched on, not disabled"


def test_the_draft_job_keeps_custom_findings(monkeypatch):
    from app import job_runner
    from app.agents.diagnostician import Diagnosis, Finding
    from tests.test_job_runner import FakeStore, _audit

    checks = {"a1": [{"code": c, "status": "fail", "note": "n"} for c in ("F1", "F2", "F3")]}
    fake = FakeStore([_audit("a1", "p1", "Peak")], {"p1": {}}, checks,
                     existing={"a1": {"findings": fe.add(_doc(), GOOD)}})
    saved = {}
    fake.save_draft_findings = lambda aid, findings, **kw: saved.update(pool=findings)
    monkeypatch.setattr(job_runner, "store", fake)
    monkeypatch.setattr(job_runner.jobs, "log", lambda *a: None)

    async def draft(**kw):
        return Diagnosis(ok=True, model="t", findings=tuple(
            Finding(check_code=c, ordinal=i, what_we_saw="x", what_it_means="y", what_fixing_takes="z")
            for i, c in enumerate(("F1", "F2", "F3"), start=1)))

    monkeypatch.setattr("app.agents.diagnostician.draft_findings", draft)
    asyncio.run(job_runner.run_draft_job("j1", {"batch_id": "b1", "only_audit_id": "a1"}))
    assert [f["code"] for f in saved["pool"]] == ["F1", "F2", "F3", "X1"]
