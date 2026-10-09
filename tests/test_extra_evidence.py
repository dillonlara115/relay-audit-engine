"""Local reach and Technical results in the findings draft."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app import job_runner
from app.agents import diagnostician, extra_evidence as ex
from app.agents.diagnostician import Diagnosis, Finding
from app.tools import reach
from tests.test_console import _prospect_page

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _reach(top_rows=1, size=5):
    """A 5 by 5 run: in the top three on the first top_rows rows (the north),
    12th in the middle row, missing from the rest."""
    spots = [reach.Spot(r, c, 0, 0, rank=(2 if r < top_rows else 12 if r == 2 else None),
                        top=[{"title": "Rival Roofing", "place_id": "r1", "rating": 4.9, "reviews": 300,
                              "mine": False}])
             for r in range(size) for c in range(size)]
    return {**reach.summarize(spots, keyword="roof repair", size=size, radius_miles=5, cost=0.05),
            "status": "done", "finished_at": T0}


# ── Local reach ───────────────────────────────────────────────────────────────


def test_weak_reach_becomes_a_finding_with_measured_numbers_and_no_names():
    row, truth = ex.reach_row(_reach(top_rows=1))
    assert truth is None and row["code"] == "R1"
    note = row["note"]
    assert "in the top three at 5, listed in the first 20 at 10, missing from the first 20 at 15" in note
    assert "At the business itself: number 12." in note
    assert "north 5 of 10" in note and "south 0 of 10" in note
    assert "Rival Roofing" not in note and "Never name another business" in note and "300 reviews" in note


def test_strong_reach_is_ground_truth_not_a_finding():
    row, truth = ex.reach_row(_reach(top_rows=4))
    assert row is None and "20 of 25" in truth and "Do not say they are hard to find" in truth


def test_no_run_or_a_failed_run_adds_nothing():
    assert ex.reach_row({}) == (None, None)
    assert ex.reach_row({"status": "failed", "error": "x"}) == (None, None)


# ── The crawl ─────────────────────────────────────────────────────────────────


def test_the_crawl_adds_only_problems_an_owner_or_google_would_trip_on():
    tech = {"status": "done", "pages_crawled": 46, "issues": [
        {"key": "broken_links", "label": "Broken links", "count": 7},
        {"key": "no_favicon", "label": "Pages with no favicon", "count": 46},
        {"key": "no_title", "label": "Pages with no title", "count": 2}]}
    row = ex.crawl_row(tech)
    assert row["code"] == "T1" and "Crawled 46 pages" in row["note"]
    assert "Broken links: 7" in row["note"] and "Pages with no title: 2" in row["note"] and "favicon" not in row["note"]
    assert ex.crawl_row({"status": "done", "issues": [{"key": "no_favicon", "label": "x", "count": 3}]}) is None
    assert ex.crawl_row({"status": "crawling"}) is None


# ── Speed ─────────────────────────────────────────────────────────────────────


def _lh(perf, lcp, at=T0 + timedelta(days=2)):
    return {"scores": {"performance": perf}, "metrics": {"lcp_ms": lcp, "lcp_source": "field",
                                                         "speed_index_ms": 5100}, "measured_at": at}


FAIL_C3 = {"code": "C3", "title": "Mobile speed", "points": 2, "note": "PSI 31"}
FAIL_B1 = {"code": "B1", "title": "Self-serve booking", "points": 10, "note": "none"}
AUDITED = {"started_at": T0, "finished_at": T0}


def test_failing_speed_checks_get_the_numbers_but_never_the_rating():
    failures, _, used = ex.merge({**AUDITED, "lighthouse": _lh(31, 6800)}, [FAIL_C3, FAIL_B1], [])
    c3 = next(f for f in failures if f["code"] == "C3")
    assert "6.8 s to appear for real visitors" in c3["note"] and "5.1 s" in c3["note"]
    assert "31" not in c3["note"].replace("PSI 31", ""), "Google's 0 to 100 rating stays out"
    assert used == ["lighthouse"]


def test_a_retest_that_says_fast_drops_the_old_speed_failures():
    failures, passing, _ = ex.merge({**AUDITED, "lighthouse": _lh(88, 1900)}, [FAIL_C3, FAIL_B1], [])
    assert [f["code"] for f in failures] == ["B1"]
    assert any(p["code"] == "T2" and "do not call it slow" in p["note"] for p in passing)


def test_the_audits_own_test_never_overrules_its_own_checks():
    """Same measurement, not a retest: the failure stands."""
    failures, _, _ = ex.merge({**AUDITED, "lighthouse": _lh(88, 1900, at=T0)}, [FAIL_C3], [])
    assert [f["code"] for f in failures] == ["C3"]


def test_a_retest_that_says_slow_adds_a_finding_when_the_checks_passed():
    failures, _, _ = ex.merge({**AUDITED, "lighthouse": _lh(30, 7000)}, [FAIL_B1], [])
    assert [f["code"] for f in failures] == ["B1", "T2"]


def test_everything_together_stays_sorted_worst_first():
    audit = {**AUDITED, "local_reach": _reach(), "lighthouse": _lh(31, 6800),
             "technical": {"status": "done", "pages_crawled": 9,
                           "issues": [{"key": "broken_links", "label": "Broken links", "count": 2}]}}
    failures, _, used = ex.merge(audit, [FAIL_C3, FAIL_B1], [])
    assert [f["code"] for f in failures] == ["B1", "R1", "C3", "T1"]
    assert used == ["local_reach", "crawl", "lighthouse"]


# ── The draft ─────────────────────────────────────────────────────────────────


def test_the_prompt_forbids_naming_rivals_and_saying_one_thing_twice():
    assert "Never name another business" in diagnostician.PROMPT
    assert "R1 and F8 are both about showing up in Google Maps" in diagnostician.PROMPT


def test_a_finding_may_cite_an_extra_evidence_code():
    raw = {"findings": [{"check_code": c, "what_we_saw": "a", "what_it_means": "b", "what_fixing_takes": "c"}
                        for c in ("R1", "T1", "B1")]}
    assert diagnostician.parse_diagnosis(raw, valid_codes=["R1", "T1", "B1"]).ok


def test_the_draft_job_feeds_reach_and_the_crawl_to_the_model(monkeypatch):
    from tests.test_job_runner import FakeStore, _audit

    audit = {**_audit("a1", "p1", "Peak Roofing"), "local_reach": _reach(),
             "technical": {"status": "done", "pages_crawled": 9,
                           "issues": [{"key": "broken_links", "label": "Broken links", "count": 2}]}}
    checks = {"a1": [{"code": c, "status": "fail", "note": "n"} for c in ("F1", "F2", "F3")]}
    fake = FakeStore([audit], {"p1": {}}, checks)
    monkeypatch.setattr(job_runner, "store", fake)
    monkeypatch.setattr(job_runner.jobs, "log", lambda job_id, line: None)
    seen = {}

    async def fake_draft(*, business_name, city, failures, passing=()):
        seen["codes"] = [f["code"] for f in failures]
        return Diagnosis(ok=True, model="t", findings=tuple(
            Finding(check_code=c, ordinal=i, what_we_saw="x", what_it_means="y", what_fixing_takes="z")
            for i, c in enumerate(("R1", "F1", "T1"), start=1)))

    monkeypatch.setattr("app.agents.diagnostician.draft_findings", fake_draft)
    asyncio.run(job_runner.run_draft_job("j1", {"batch_id": "b1", "only_audit_id": "a1"}))
    assert {"R1", "T1"} <= set(seen["codes"]) and fake.sources == ["local_reach", "crawl"]


# ── The console ───────────────────────────────────────────────────────────────


def _doc(drafted_at=T0, **over):
    return {"status": "draft", "drafted_at": drafted_at, "findings": [
        {"code": c, "ordinal": i, "what_we_saw": f"saw {c}", "what_it_means": "m", "what_fixing_takes": "f"}
        for i, c in enumerate(("R1", "T1", "B1", "F3"), start=1)], **over}


def test_cards_say_where_a_finding_came_from():
    page = _prospect_page(findings=_doc())
    assert "From Local reach" in page and "From Technical" in page
    assert page.count("<p class=\"muted text-sm mt-1 mb-0\">From ") == 2


def test_a_reach_run_after_the_draft_offers_a_redraft():
    page = _prospect_page(findings=_doc(), audit={"local_reach": {"finished_at": T0 + timedelta(hours=1)}})
    assert "Local reach ran after these were drafted" in page
    assert 'action="/console/audits/a1/draft"' in page and "Draft findings again" in page


def test_no_redraft_once_the_report_is_out():
    page = _prospect_page(findings=_doc(status="approved", selected=[1, 2, 3]),
                          audit={"report_slug": "abc", "local_reach": {"finished_at": T0 + timedelta(hours=1)}})
    assert "ran after these were drafted" not in page and "Draft findings again" not in page


def test_runs_before_the_draft_raise_nothing():
    page = _prospect_page(findings=_doc(), audit={"local_reach": {"finished_at": T0 - timedelta(hours=1)}})
    assert "ran after these were drafted" not in page
