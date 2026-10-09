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
    def __init__(self, audits, prospects, checks, existing=None):
        self._audits = audits
        self._prospects = prospects
        self._checks = checks
        self._existing = existing or {}  # audit_id -> findings doc already on record
        self.drafted_for: list[str] = []

    def get_draft_findings(self, audit_id):
        return self._existing.get(audit_id)

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

    async def fake_draft_findings(*, business_name, city, failures, passing=()):
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

    async def fake_draft_findings(*, business_name, city, failures, passing=()):
        return Diagnosis(ok=True, findings=(_finding("F1", 1), _finding("F2", 2), _finding("F3", 3)),
                         model="test")

    monkeypatch.setattr("app.agents.diagnostician.draft_findings", fake_draft_findings)

    result = asyncio.run(job_runner.run_draft_job("job1", {"batch_id": "b1", "top": 40}))

    assert set(fake.drafted_for) == {"a1", "a2"}
    assert result == {"batch_id": "b1", "drafted": 2, "skipped": 0}


# ── Bulk drafting never overwrites ────────────────────────────────────────────


def _bulk_env(monkeypatch, n=4, existing=None):
    names = ["Peak", "Summit", "Ridge", "Crest", "Gable", "Eave"][:n]
    audits = [_audit(f"a{i}", f"p{i}", f"{name} Roofing") for i, name in enumerate(names, start=1)]
    failing = [{"code": c, "status": "fail", "note": "n"} for c in ("F1", "F2", "F3")]
    fake = FakeStore(audits, {f"p{i}": {} for i in range(1, n + 1)},
                     {f"a{i}": failing for i in range(1, n + 1)}, existing=existing)
    monkeypatch.setattr(job_runner, "store", fake)

    async def fake_draft_findings(*, business_name, city, failures, passing=()):
        return Diagnosis(ok=True, findings=(_finding("F1", 1), _finding("F2", 2), _finding("F3", 3)),
                         model="test")
    monkeypatch.setattr("app.agents.diagnostician.draft_findings", fake_draft_findings)
    return fake


def test_bulk_skips_prospects_that_already_have_findings(monkeypatch):
    """Regression: drafting the top 10 again overwrote approved findings on a
    published prospect, scrambling which finding each follow-up email carries."""
    fake = _bulk_env(monkeypatch, existing={"a1": {"status": "approved"}, "a2": {"status": "draft"}})
    result = asyncio.run(job_runner.run_draft_job("job1", {"batch_id": "b1", "top": 0}))
    assert set(fake.drafted_for) == {"a3", "a4"}
    assert result["drafted"] == 2


def test_top_n_counts_only_prospects_without_findings(monkeypatch):
    fake = _bulk_env(monkeypatch, existing={"a1": {"status": "approved"}})
    asyncio.run(job_runner.run_draft_job("job1", {"batch_id": "b1", "top": 2}))
    assert len(fake.drafted_for) == 2 and "a1" not in fake.drafted_for


def test_selected_prospects_are_drafted_and_nothing_else(monkeypatch):
    fake = _bulk_env(monkeypatch, existing={"a3": {"status": "approved"}})
    asyncio.run(job_runner.run_draft_job("job1", {"batch_id": "b1", "audit_ids": ["a2", "a3", "a4"]}))
    assert set(fake.drafted_for) == {"a2", "a4"}, "a3 is selected but already has findings"


def test_a_redelivered_job_picks_up_where_it_left_off(monkeypatch):
    """Pub/Sub redelivers a job that outlives its ack deadline. Skipping what
    is already drafted makes the second run finish the job, not repeat it."""
    fake = _bulk_env(monkeypatch, existing={"a1": {"status": "draft"}, "a2": {"status": "draft"}})
    asyncio.run(job_runner.run_draft_job("job1", {"batch_id": "b1", "top": 0}))
    assert set(fake.drafted_for) == {"a3", "a4"}


def test_drafting_from_one_prospect_page_may_redraft_it(monkeypatch):
    fake = _bulk_env(monkeypatch, existing={"a2": {"status": "draft"}})
    asyncio.run(job_runner.run_draft_job("job1", {"batch_id": "b1", "only_audit_id": "a2"}))
    assert fake.drafted_for == ["a2"]
