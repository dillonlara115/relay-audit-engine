"""Tools on a site that can make our results read wrong, and the notes the
console and the findings draft take from them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents import extra_evidence as ex
from app.tools import stack
from tests.test_console import _prospect_page

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_fingerprints_in_markup_titles_and_the_crawls_tech_read():
    assert stack.detect('<script src="https://cdn-abc.nitrocdn.com/x.js"></script>') == ["nitropack"]
    assert stack.detect("", "Robot Challenge Screen") == ["siteground_check"]
    assert stack.detect('<link href="/wp-content/cache/wp-rocket/x.css">') == ["wp_rocket"]
    assert stack.detect("<title>Just a moment...</title>") == ["cloudflare_check"]
    assert stack.detect("<html><body>Roofing in Pueblo</body></html>") == []
    assert stack.detect(None, "") == []


def test_everything_known_for_an_audit_is_combined_once():
    audit = {"stack": {"tools": ["nitropack"]},
             "technical": {"cms": "nitropack", "needs_browser": True}}
    labels = [t.label for t in stack.found_on(audit)]
    assert labels == ["NitroPack", "Robot check"]
    assert [t.label for t in stack.found_on({"technical": {"cms": "elementor 4.3.4"}})] == []


def test_the_technical_section_explains_each_tool():
    page = _prospect_page(audit={"technical": {"cms": "nitropack", "status": "done", "health": 93,
                                               "pages_crawled": 50, "issues": [], "site": []}})
    assert "Read these results with care" in page and "<b>NitroPack.</b>" in page
    assert "Serves speed tests an optimised copy" in page


def test_findings_get_a_short_note_when_checks_could_read_wrong():
    doc = {"status": "draft", "drafted_at": T0, "findings": [
        {"code": "B4", "ordinal": i, "what_we_saw": "s", "what_it_means": "m", "what_fixing_takes": "f"}
        for i in (1, 2, 3)]}
    page = _prospect_page(findings=doc, audit={"stack": {"tools": ["siteground_check"]}})
    assert "this site uses SiteGround robot check, which can make some checks read wrong" in page


def test_no_tools_no_notes():
    page = _prospect_page()
    assert "Read these results with care" not in page and "Read with care:" not in page


def test_a_flattered_lab_test_never_clears_a_speed_failure():
    lh = {"scores": {"performance": 95}, "metrics": {"lcp_ms": 1500, "lcp_source": "lab"},
          "measured_at": T0 + timedelta(days=1)}
    fail = {"code": "C3", "title": "Mobile speed", "points": 2, "note": "slow"}
    audit = {"started_at": T0, "finished_at": T0, "lighthouse": lh, "technical": {"cms": "nitropack"}}
    failures, _, _ = ex.merge(audit, [fail], [])
    assert [f["code"] for f in failures] == ["C3"]
    # Real visitors' timings still count, NitroPack or not.
    audit["lighthouse"] = {**lh, "metrics": {"lcp_ms": 1500, "lcp_source": "field"}}
    failures, _, _ = ex.merge(audit, [fail], [])
    assert failures == []


def test_word_counts_are_left_out_when_text_is_held_back():
    tech = {"status": "done", "pages_crawled": 50, "cms": "nitropack", "issues": [
        {"key": "low_content_rate", "label": "Thin pages, little text", "count": 47},
        {"key": "broken_resources", "label": "Broken images or files", "count": 24}]}
    failures, _, _ = ex.merge({"technical": tech}, [], [])
    note = next(f["note"] for f in failures if f["code"] == "T1")
    assert "Broken images or files: 24" in note and "Thin pages" not in note
    failures, _, _ = ex.merge({"technical": {**tech, "cms": ""}}, [], [])
    assert "Thin pages" in next(f["note"] for f in failures if f["code"] == "T1")


def test_every_audit_records_what_its_render_saw(monkeypatch):
    import asyncio

    from app import pipeline
    from app.scoring import compute
    from app.tools.render import RenderResult

    writes = []
    monkeypatch.setattr(pipeline.store, "create_audit", lambda pid, bid: "a1")
    monkeypatch.setattr(pipeline.store, "write_check_results", lambda aid, rows: None)
    monkeypatch.setattr(pipeline.store, "update_audit", lambda aid, f: writes.append(f))
    render = RenderResult(ok=False, url="u", title="Robot Challenge Screen", html="<div>sgcaptcha</div>")
    asyncio.run(pipeline.persist_audit(prospect={"place_id": "p1"}, batch_id="b1", score=compute([]),
                                       results={}, definitions=[], crawl_error=None, pages_crawled=0,
                                       render=render))
    assert writes[-1]["stack"] == {"tools": ["siteground_check"]}
