"""audit_doc_id: one audit per prospect per batch.

Regression coverage for a real bug: create_audit used to mint a fresh random
document id on every call, so re-auditing a prospect added a second row to
its batch's call list instead of replacing the first. The fix keys the audit
document deterministically off (batch_id, prospect_id) so a re-audit
overwrites in place.
"""

from __future__ import annotations

from app.store.firestore import audit_doc_id


def test_same_batch_and_prospect_always_gets_the_same_doc_id():
    first = audit_doc_id("prospect-1", "batch-a")
    second = audit_doc_id("prospect-1", "batch-a")
    assert first == second


def test_different_prospects_in_the_same_batch_get_different_ids():
    assert audit_doc_id("prospect-1", "batch-a") != audit_doc_id("prospect-2", "batch-a")


def test_the_same_prospect_in_different_batches_gets_different_ids():
    assert audit_doc_id("prospect-1", "batch-a") != audit_doc_id("prospect-1", "batch-b")
