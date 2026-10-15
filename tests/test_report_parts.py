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
    monkeypatch.setattr(routes.store, "get_prospect", lambda pid: {"business_name": "Apex", "city": "Pueblo"})
    monkeypatch.setattr(routes.store, "audit_checks", lambda aid: env.get("checks", []))
    from app.store import evidence as evidence_store
    monkeypatch.setattr(evidence_store, "audit_evidence", lambda aid: env.get("evidence", []))
    monkeypatch.setattr(evidence_store, "freeze_copy", lambda src, dest: env.setdefault("frozen", []).append((src, dest)) or dest)
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
    check = extras.site_check_snapshot(TECH, with_items=False)
    assert [line["text"] for line in check["lines"]] == ["7 links lead nowhere", "1 page has no title for Google to show"]
    assert "favicon" not in json.dumps(check)
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
    assert 'name="site_check" value="1" checked>' in page and "On by default" in page, \
        "no choice saved yet, so the page check is on by default"


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


# ── The actual pages, and on by default ──────────────────────────────────────

from app.tools import onpage  # noqa: E402

DETAILS = {
    "broken_links": {"total": 9, "columns": [], "rows": [
        ["https://a.com/old-page/", "404", "https://a.com/roof-repair/", "Get a quote"],
        ["https://a.com/gone/", "404", "https://a.com/", ""]]},
    "broken_resources": {"total": 2, "columns": [], "rows": [
        ["https://a.com/wp-content/uploads/hail.png", "404", "https://a.com/storm/ (+2 more)"],
        ["https://cdn.x.com/y.js", "no answer", ""]]},
    "no_title": {"total": 1, "columns": [], "rows": [["https://a.com/thanks/", ""]]},
}
TECH2 = {"status": "done", "pages_crawled": 40, "health": 81, "task_id": "T1", "issues": [
    {"key": "broken_links", "count": 9}, {"key": "broken_resources", "count": 2}, {"key": "no_title", "count": 1}]}


def test_each_problem_names_its_pages(monkeypatch):
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: DETAILS[key])
    check = extras.site_check_snapshot(TECH2)
    links, files, titles = check["lines"]
    assert links["items"][0] == {"link": "/roof-repair/", "href": "https://a.com/roof-repair/",
                                 "note": "links to /old-page/ (\u201cGet a quote\u201d)"}
    assert links["more"] == 7, "9 in all, 2 shown"
    assert files["items"][0] == {"link": "/storm/", "href": "https://a.com/storm/", "note": "hail.png doesn't load, on 3 pages"}
    assert files["items"][1] == {"link": "y.js", "href": None, "note": "doesn't load"}
    assert titles["items"] == [{"link": "/thanks/", "href": "https://a.com/thanks/", "note": ""}]


def test_their_words_are_held_to_our_copy_rules(monkeypatch):
    rows = [["https://a.com/x/", "404", "https://a.com/a/", "Hail \u2014 storm help"],
            ["https://a.com/y/", "404", "https://a.com/b/", "Check your roof score"]]
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: {"total": 2, "rows": rows})
    items, total = extras.items_for("broken_links", {"total": 2, "rows": rows})
    assert len(items) == 1 and "\u2014" not in items[0]["note"] and total == 2


def test_a_crawl_that_cannot_be_read_still_gives_the_counts(monkeypatch):
    def gone(task, key):
        raise onpage.OnPageUnavailable("expired")
    monkeypatch.setattr(onpage, "cached_detail", gone)
    check = extras.site_check_snapshot(TECH2)
    assert [line["items"] for line in check["lines"]] == [[], [], []] and check["lines"][0]["text"] == "9 links lead nowhere"


def test_the_report_lists_the_pages_under_each_problem(monkeypatch):
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: DETAILS[key])
    page = render_report(report(site_check=extras.site_check_snapshot(TECH2)))
    assert '<a href="https://a.com/roof-repair/">/roof-repair/</a>: links to /old-page/' in page
    assert "and 7 more" in page and "y.js: doesn" in page
    assert forbidden_terms_in(page) == [] and not contains_forbidden_dash(page)


def test_an_older_snapshot_of_plain_lines_still_renders():
    page = render_report(report(site_check={"pages": 3, "health": 90, "lines": ["2 links lead nowhere"]}))
    assert "<li>2 links lead nowhere</li>" in page


def test_on_by_default_until_someone_chooses(monkeypatch):
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: DETAILS[key])
    write = extras.default_site_check({"technical": TECH2})
    assert write["report_extras"]["default"] is True and len(write["report_extras"]["site_check"]["lines"]) == 3
    assert extras.default_site_check({"technical": TECH2, "report_extras": {"chosen": True}}) is None
    assert extras.default_site_check({"technical": TECH2, "report_slug": "abc"}) is None, "never onto a report already out"
    assert extras.default_site_check({"technical": {"status": "crawling"}}) is None


def test_saving_marks_a_choice(client, pool_store, monkeypatch):
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: DETAILS[key])
    pool_store["audit"] = {"prospect_id": "p1", "technical": TECH2}
    csrf = sign_in(client)
    client.post("/console/audits/a1/report-sections", data={"csrf": csrf}, follow_redirects=False)
    saved = pool_store["saved"][-1]["report_extras"]
    assert saved["chosen"] is True and saved["site_check"] is None and saved["default"] is False


def test_publishing_a_first_report_adds_the_page_check(monkeypatch):
    from app.report import publish as pub

    audit = {"audit_id": "a1", "prospect_id": "p1", "technical": TECH2}
    writes = []
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: DETAILS[key])
    monkeypatch.setattr(pub.store, "get_audit", lambda aid: audit)
    monkeypatch.setattr(pub.store, "get_prospect", lambda pid: {"business_name": "Apex", "city": "Pueblo"})
    monkeypatch.setattr(pub.store, "load_suppressions", lambda: {})
    monkeypatch.setattr(pub.store, "suppression_hit", lambda *a, **k: None)
    monkeypatch.setattr(pub.store, "get_draft_findings", lambda aid: {
        "status": "approved", "selected": [1, 2, 3], "findings": [
            {"ordinal": i, "what_we_saw": f"s{i}", "what_it_means": "m", "what_fixing_takes": "f"} for i in (1, 2, 3)]})
    monkeypatch.setattr(pub.evidence_store, "audit_evidence", lambda aid: [{"kind": "screenshot", "gcs_path": "p"}])
    monkeypatch.setattr(pub.evidence_store, "freeze_for_report", lambda path, slug: "frozen")
    monkeypatch.setattr(pub.store, "update_audit", lambda aid, f: writes.append(f))
    pub.publish("a1")
    assert writes[0]["report_extras"]["default"] is True and "report_slug" in writes[-1]


def test_file_names_survive_long_paths_and_the_homepage_has_a_name():
    long = "https://a.com/%22http:/a.com/wp-content/uploads/2024/09/a-very-long-folder-name/Untitled-27-1024x574.png/"
    assert extras._file_name(long) == "Untitled-27-1024x574.png"
    assert extras.path_of("https://a.com/") == "Homepage"
    items, _ = extras.items_for("broken_links", {"total": 1, "rows": [["https://a.com/x/", "404", "https://a.com/", ""]]})
    assert items[0]["link"] == "Homepage"



# ── Refused is not broken (Sun Spirit Roofing, October 2026) ─────────────────

SUN = {"status": "done", "pages_crawled": 18, "health": 89, "task_id": "T9", "issues": [
    {"key": "broken_resources", "count": 18}, {"key": "no_description", "count": 18}]}
SUN_DETAIL = {
    "broken_resources": {"total": 1, "rows": [["https://ss.com/146ea4e67f.js", "403, blocked to our crawler",
                                                "https://ss.com/ (+17 more)"]]},
    "no_description": {"total": 18, "rows": [["https://ss.com/", "Home"]]},
}


def test_a_file_refused_to_our_crawler_is_not_reported_broken(monkeypatch):
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: SUN_DETAIL[key])
    check = extras.site_check_snapshot(SUN)
    assert [line["key"] for line in check["lines"]] == ["no_description"], \
        "one script answering 403 on 18 pages was '18 images or files don't load'"


def test_broken_files_are_counted_once_each(monkeypatch):
    detail = {"total": 2, "rows": [["https://a.com/x.png", "404", "https://a.com/ (+17 more)"],
                                   ["https://a.com/y.js", "403, blocked to our crawler", "https://a.com/"]]}
    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: detail)
    check = extras.site_check_snapshot({**SUN, "issues": [{"key": "broken_resources", "count": 19}]})
    (line,) = check["lines"]
    assert line["text"] == "1 image or file doesn't load" and line["items"][0]["note"] == "x.png doesn't load, on 18 pages"


def test_drafts_see_the_verified_count(monkeypatch):
    from app.agents import extra_evidence as ex

    monkeypatch.setattr(onpage, "cached_detail", lambda task, key: SUN_DETAIL[key])
    row = ex.crawl_row({**SUN, "issues": [{"key": "broken_resources", "label": "Broken images or files", "count": 18},
                                          {"key": "no_description", "label": "Pages with no meta description", "count": 18}]})
    assert "Broken images" not in row["note"] and "Pages with no meta description: 18" in row["note"]


def test_the_console_says_when_a_file_was_only_refused(dfs):
    client, _ = _routed_one({"resources": {"total_items_count": 1, "items": [{"url": "https://a.com/x.js", "status_code": 403}]},
                             "pages_by_resource": {"total_items_count": 0, "items": []}})
    d = onpage.detail("T1", "broken_resources", client=client)
    assert d["rows"][0][1] == "403, blocked to our crawler"


import httpx  # noqa: E402

from app.config import Config  # noqa: E402


@pytest.fixture()
def dfs(monkeypatch):
    monkeypatch.setattr(onpage, "get_config", lambda: Config(dataforseo_login="l", dataforseo_password="p"))


def _routed_one(responses):
    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"tasks": [{"status_code": 20000, "result": [responses[path]]}]})
    return httpx.Client(transport=httpx.MockTransport(handler)), None



# ── Rankings and the Maps grid on the report ──────────────────────────────────

CHECKS = [{"code": "F8", "status": "fail", "observed": {}},
          {"code": "F9", "status": "pass", "observed": {"organic_rank": 7}},
          {"code": "F3", "status": "fail"}]


def _reach(**over):
    pts = [{"row": r, "col": c, "lat": 38.25 + (1 - r) * 0.02, "lng": -104.6 + (c - 1) * 0.02,
            "rank": (2 if r == 0 else 12 if r == 1 else None), "top": ["Rival Roofing"], "error": ""}
           for r in range(3) for c in range(3)]
    return {"status": "done", "keyword": "roof repair", "radius_miles": 3, "size": 3, "points": pts,
            "answered": 9, "top3": 3, "found": 6, "center": {"lat": 38.25, "lng": -104.6},
            "map": {"zoom": 12, "error": ""}, "competitors": [{"title": "Rival Roofing", "spots": 9, "rating": 4.9, "reviews": 300}], **over}


def test_rankings_come_from_searches_already_made():
    pos = extras.positions_snapshot(CHECKS, city="Pueblo", reach=_reach())
    assert pos["rows"] == [
        {"kind": "maps", "query": "roofer Pueblo", "rank": None},
        {"kind": "search", "query": "roof replacement Pueblo", "rank": 7},
        {"kind": "grid", "query": "roof repair", "top3": 3, "answered": 9, "radius_miles": 3}]
    assert extras.positions_snapshot([{"code": "F8", "status": "skip"}], city="Pueblo") is None


def test_the_maps_grid_never_names_another_business():
    snap = extras.reach_snapshot(_reach(), map_path="snapshots/a1/reach-map.png")
    assert len(snap["pins"]) == 9 and snap["pins"][4]["center"] and snap["missing"] == 3
    assert "x" in snap["pins"][0] and snap["map_path"] == "snapshots/a1/reach-map.png"
    assert "Rival" not in json.dumps(snap)
    page = render_report(report(reach=snap, reach_map_url="https://storage.example/m.png"))
    assert "Where you show up in Google Maps" in page and 'src="https://storage.example/m.png"' in page
    assert page.count('class="pin ') == 9 and "At 3, you're not in the first 20 results." in page and "Rival" not in page
    assert "At 3 spots, you're in the top 3, where most homeowners look." in page
    assert forbidden_terms_in(page) == [] and not contains_forbidden_dash(page)
    assert "map_path" not in json.dumps(report(reach=snap).to_dict())


def test_without_a_map_the_grid_still_shows():
    snap = extras.reach_snapshot(_reach(map={"zoom": 12, "error": "403"}), map_path=None)
    page = render_report(report(reach=snap))
    assert 'class="gridonly"' in page and "<img" not in page.split("Where you show up")[1].split("</p>")[1]


def test_the_rankings_table_renders():
    page = render_report(report(positions=extras.positions_snapshot(CHECKS, city="Pueblo")))
    assert "Where you show up on Google" in page and '<td class="place">7th on the page</td>' in page
    assert "What a homeowner searches" in page and "\u201croofer Pueblo\u201d" in page and "Not in the top 3" in page
    assert forbidden_terms_in(page) == []


def test_saving_the_grid_freezes_its_own_map_copy(client, pool_store):
    pool_store["audit"] = {"prospect_id": "p1", "local_reach": _reach()}
    pool_store["checks"] = CHECKS
    pool_store["evidence"] = [{"kind": "reach_map", "gcs_path": "evidence/p1/a1/reach-map.png"}]
    csrf = sign_in(client)
    r = client.post("/console/audits/a1/report-sections",
                    data={"csrf": csrf, "positions": "1", "reach": "1"}, follow_redirects=False)
    assert "notice=sections_saved" in r.headers["location"]
    (src, dest), = pool_store["frozen"]
    assert src == "evidence/p1/a1/reach-map.png" and dest.startswith("snapshots/a1/reach-map-")
    saved = pool_store["saved"][-1]["report_extras"]
    assert saved["reach"]["map_path"] == dest and saved["positions"]["rows"][1]["rank"] == 7


def test_the_panel_offers_both_new_sections():
    page = _prospect_page(audit={"local_reach": _reach()})
    assert 'name="positions" value="1"' in page and 'name="reach" value="1">' in page
    assert "Run Local reach first" not in page
    assert 'name="reach" value="1" disabled' in _prospect_page()


def test_a_published_report_signs_the_frozen_map(monkeypatch):
    from app.report import publish as pub

    snap = extras.reach_snapshot(_reach(), map_path="snapshots/a1/reach-map.png")
    audit = {"audit_id": "a1", "prospect_id": "p1", "published_at": T0, "report_extras": {"reach": snap}}
    monkeypatch.setattr(pub, "load_by_slug", lambda slug: (audit, {"business_name": "Apex", "city": "Pueblo"}, {
        "status": "approved", "selected": [1, 2, 3], "findings": [
            {"ordinal": i, "what_we_saw": f"s{i}", "what_it_means": "m", "what_fixing_takes": "f"} for i in (1, 2, 3)]}))
    monkeypatch.setattr(pub.evidence_store, "audit_evidence", lambda aid: [])
    monkeypatch.setattr(pub.evidence_store, "signed_url", lambda path: f"https://signed.example/{path}")
    page = pub.render_by_slug("slug")
    assert 'src="https://signed.example/snapshots/a1/reach-map.png"' in page



# ── Plain words (the "Top 3 at 0 of 15" report, October 2026) ─────────────────

from app.report.template import _nth, _position_row  # noqa: E402


def test_none_in_the_top_three_is_said_plainly():
    row = {"kind": "grid", "query": "roofer", "top3": 0, "answered": 15, "radius_miles": 5}
    assert _position_row(row) == ("roofer", "Google Maps, from 15 spots within 5 miles of you",
                                  "Not in the top 3 at any of the 15 spots")
    page = render_report(report(positions={"rows": [row]}))
    assert "Top 3 at 0" not in page


def test_positions_read_as_places_on_a_list():
    assert [_nth(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"]
    assert _position_row({"kind": "maps", "query": "roofer Prescott", "rank": 2})[2] == "2nd on the list"
    assert _position_row({"kind": "search", "query": "x", "rank": None})[2] == "Not on the first two pages"


def test_spots_whose_search_failed_are_left_off_the_map():
    snap = extras.reach_snapshot(_reach(), map_path=None)
    snap["pins"][0]["band"], snap["pins"][0]["label"] = "none", "?"
    page = render_report(report(reach=snap))
    assert ">?<" not in page and page.count('class="pin ') == 9 and page.count('class="pin empty') == 1, \
        "drawn empty so the grid keeps its shape"
    assert "No roofers listed there" in page


def test_a_town_centred_grid_says_so():
    snap = extras.reach_snapshot({**_reach(), "center_on": "town", "center_label": "Prescott"}, map_path=None)
    page = render_report(report(reach=snap))
    assert "from spots all around Prescott" in page and "Circled: the middle of Prescott" in page


def test_no_top_three_anywhere_says_so():
    snap = extras.reach_snapshot(_reach(top3=0, found=4, answered=9), map_path=None)
    page = render_report(report(reach=snap))
    assert "At none of these spots are you in the top 3" in page and "At 4, you're further down the list." in page


def test_rows_saved_in_the_old_wording_still_show():
    page = render_report(report(positions={"rows": [{"search": "A roofer in Pueblo, on Google Maps", "place": "Number 2"}]}))
    assert "A roofer in Pueblo, on Google Maps" in page and "Number 2" in page
