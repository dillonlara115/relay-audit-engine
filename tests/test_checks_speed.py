"""C3, C4, and the PSI flattener. No network.

The flattener is where the lab-versus-field decision lives, so it gets the
table-driven treatment: that choice changes what we tell a contractor his
customers experience.
"""

from __future__ import annotations

import pytest

from app.checks import speed  # noqa: F401 - registers the checks
from app.checks.base import REGISTRY, AuditContext
from app.checks.extract import SiteFacts
from app.scoring import FAIL, PASS, SKIPPED
from app.tools.pagespeed import FIELD, LAB, PsiResult, flatten

URL = "https://peakroofing.com/"


def payload(*, score=0.85, lab_lcp=2100.0, field_lcp=None, field_category=None):
    body = {
        "lighthouseResult": {
            "requestedUrl": URL,
            "finalUrl": URL,
            "categories": {"performance": {"score": score}},
            "audits": {
                "largest-contentful-paint": {"numericValue": lab_lcp},
                "first-contentful-paint": {"numericValue": 1200.0},
                "cumulative-layout-shift": {"numericValue": 0.05},
                "total-blocking-time": {"numericValue": 210.0},
            },
        }
    }
    if field_lcp is not None:
        body["loadingExperience"] = {
            "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {
                "percentile": field_lcp, "category": field_category or "FAST"}}
        }
    return body


def psi(**overrides) -> PsiResult:
    base = dict(ok=True, url=URL, performance_score=85, lcp_ms=2100.0, lcp_source=LAB,
                lab_lcp_ms=2100.0)
    base.update(overrides)
    return PsiResult(**base)


def ctx(psi_result=None) -> AuditContext:
    return AuditContext(place={}, site=SiteFacts(homepage=None), psi=psi_result)


def run(code, psi_result):
    return REGISTRY[code](ctx(psi_result))


# ── Flattening ────────────────────────────────────────────────────────────────


def test_score_is_scaled_to_a_hundred():
    assert flatten(payload(score=0.4), URL, "mobile").performance_score == 40
    assert flatten(payload(score=1.0), URL, "mobile").performance_score == 100
    assert flatten(payload(score=0.0), URL, "mobile").performance_score == 0


def test_field_data_wins_over_lab_when_chrome_has_it():
    """Real visitors beat a simulated phone. Both are kept on the record."""
    result = flatten(payload(lab_lcp=5000.0, field_lcp=1800.0, field_category="FAST"),
                     URL, "mobile")
    assert result.lcp_ms == 1800.0
    assert result.lcp_source == FIELD
    assert result.lab_lcp_ms == 5000.0
    assert result.field_lcp_category == "FAST"


def test_lab_is_used_when_the_origin_has_no_field_data():
    """Measured on a real site: a small roofer has too little Chrome traffic
    for CrUX to report, so the lab number is all there is."""
    result = flatten(payload(lab_lcp=19351.0), URL, "mobile")
    assert result.lcp_ms == 19351.0
    assert result.lcp_source == LAB
    assert result.field_lcp_ms is None


def test_a_payload_with_nothing_usable_is_not_ok():
    assert flatten({}, URL, "mobile").ok is False


def test_a_missing_score_still_yields_a_usable_lcp():
    body = payload()
    body["lighthouseResult"]["categories"] = {}
    result = flatten(body, URL, "mobile")
    assert result.performance_score is None
    assert result.ok is True and result.lcp_ms == 2100.0


def test_to_dict_drops_absent_fields():
    row = flatten(payload(), URL, "mobile").to_dict()
    assert "field_lcp_ms" not in row, "absent is absent, never null"
    assert row["performance_score"] == 85


# ── Preconditions ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["C3", "C4"])
def test_no_psi_skips(code):
    res = run(code, None)
    assert res.status == SKIPPED
    assert "not measured" in res.note


@pytest.mark.parametrize("code", ["C3", "C4"])
def test_a_failed_psi_call_skips_rather_than_failing_the_site(code):
    """A quota error is our problem, not a defect in his website."""
    res = run(code, PsiResult(ok=False, url=URL, error="PSI returned 429: quota"))
    assert res.status == SKIPPED
    assert res.observed["error"].startswith("PSI returned 429")


# ── C3 mobile speed ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("score, status", [(100, PASS), (60, PASS), (59, FAIL), (40, FAIL), (0, FAIL)])
def test_c3_threshold(score, status):
    assert run("C3", psi(performance_score=score)).status == status


def test_c3_note_quotes_the_score():
    assert "40 out of 100" in run("C3", psi(performance_score=40)).note


def test_c3_without_a_score_skips():
    assert run("C3", psi(performance_score=None)).status == SKIPPED


# ── C4 LCP ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "lcp, status", [(1000.0, PASS), (2499.0, PASS), (2500.0, FAIL), (19351.0, FAIL)]
)
def test_c4_threshold(lcp, status):
    assert run("C4", psi(lcp_ms=lcp)).status == status


def test_c4_says_whose_experience_it_is_reporting():
    """Reporting a lab number as if it were his customers would be a claim we
    did not measure."""
    field = run("C4", psi(lcp_ms=1800.0, lcp_source=FIELD, field_lcp_ms=1800.0))
    assert "real visitors on phones wait" in field.note

    lab = run("C4", psi(lcp_ms=1800.0, lcp_source=LAB))
    assert "a test phone waits" in lab.note


def test_c4_reports_seconds_not_milliseconds():
    res = run("C4", psi(lcp_ms=19351.0))
    assert "19.4 seconds" in res.note
    assert res.observed["lcp_seconds"] == 19.35


def test_c4_keeps_both_numbers_on_the_record():
    res = run("C4", psi(lcp_ms=1800.0, lcp_source=FIELD, field_lcp_ms=1800.0, lab_lcp_ms=5000.0))
    assert res.observed["field_lcp_ms"] == 1800
    assert res.observed["lab_lcp_ms"] == 5000
    assert res.observed["source"] == FIELD


def test_c4_without_an_lcp_skips():
    assert run("C4", psi(lcp_ms=None)).status == SKIPPED
