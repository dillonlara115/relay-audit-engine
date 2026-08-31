"""The draft job: what gets drafted, and for whom.

Regression coverage for a real bug: the single-audit "Write talking points"
button in the console passes only_audit_id, but run_draft_job ignored it and
always drafted the whole batch's top N. Clicking it on one prospect silently
drafted findings for up to 40.
"""

from __future__ import annotations

import asyncio

import pytest

from app import job_runner
from app.agents.diagnostician import Diagnosis, Finding


def _audit(audit_id: str, prospect_id: str, business_name: str) -> dict:
    return {
        "audit_id": audit_id,
        "prospect_id": prospect_id,
        "batch_id": "b1",
        "segment": "leaky_bucket",
        "scores": {"booked": 0, "found": 10},
    }


def _finding(code: str, ordinal: int) -> Finding:
    return Finding(
        check_code=code, ordinal=ordinal,
        what_we_saw="x", what_it_means="y", what_fixing_takes="z",
    )


class FakeStore:
    def __init__(self, audits, prospects, checks):
        self._audits = audits
        self._prospects = prospects
        self._checks = checks
        self.drafted_for: list[str] = []

    def audits_for_batch(self, batch_id):
        return iter(self._audits)

    def get_prospect(self, place_id):
        return self._prospects.get(place_id)

    def load_suppressions(self):
        return {}

    def suppression_hit(self, suppressions, **kwargs):
        return None

    def all_check_defs(self):
        return [{"code": "F1", "title": "t", "points": 5},
                {"code": "F2", "title": "t", "points": 5},
                {"code": "F3", "title": "t", "points": 5}]

    def audit_checks(self, audit_id):
        return self._checks.get(audit_id, [])

    def save_draft_findings(self, audit_id, findings, *, needs_review, model):
        self.drafted_for.append(audit_id)


@pytest.fixture(autouse=True)
def _quiet_job_log(monkeypatch):
    monkeypatch.setattr(job_runner.jobs, "log", lambda job_id, line: None)


def test_only_audit_id_drafts_just_that_one_audit(monkeypatch):
    audits = [_audit("a1", "p1", "Peak Roofing"), _audit("a2", "p2", "Summit Roofing"),
              _audit("a3", "p3", "Ridge Roofing")]
    prospects = {"p1": {}, "p2": {}, "p3": {}}
    failing_checks = [
        {"code": "F1", "status": "fail", "note": "n"},
        {"code": "F2", "status": "fail", "note": "n"},
        {"code": "F3", "status": "fail", "note": "n"},
    ]
    checks = {"a1": failing_checks, "a2": failing_checks, "a3": failing_checks}
    fake = FakeStore(audits, prospects, checks)
    monkeypatch.setattr(job_runner, "store", fake)

    async def fake_draft_findings(*, business_name, city, failures):
        return Diagnosis(ok=True, findings=(_finding("F1", 1), _finding("F2", 2), _finding("F3", 3)),
                         model="test")

    monkeypatch.setattr("app.agents.diagnostician.draft_findings", fake_draft_findings)

    result = asyncio.run(job_runner.run_draft_job(
        "job1", {"batch_id": "b1", "top": 40, "only_audit_id": "a2"}))

    assert fake.drafted_for == ["a2"]
    assert result == {"batch_id": "b1", "drafted": 1, "skipped": 0}


def test_without_only_audit_id_drafts_the_top_n(monkeypatch):
    audits = [_audit("a1", "p1", "Peak Roofing"), _audit("a2", "p2", "Summit Roofing")]
    prospects = {"p1": {}, "p2": {}}
    failing_checks = [
        {"code": "F1", "status": "fail", "note": "n"},
        {"code": "F2", "status": "fail", "note": "n"},
        {"code": "F3", "status": "fail", "note": "n"},
    ]
    checks = {"a1": failing_checks, "a2": failing_checks}
    fake = FakeStore(audits, prospects, checks)
    monkeypatch.setattr(job_runner, "store", fake)

    async def fake_draft_findings(*, business_name, city, failures):
        return Diagnosis(ok=True, findings=(_finding("F1", 1), _finding("F2", 2), _finding("F3", 3)),
                         model="test")

    monkeypatch.setattr("app.agents.diagnostician.draft_findings", fake_draft_findings)

    result = asyncio.run(job_runner.run_draft_job("job1", {"batch_id": "b1", "top": 40}))

    assert set(fake.drafted_for) == {"a1", "a2"}
    assert result == {"batch_id": "b1", "drafted": 2, "skipped": 0}
