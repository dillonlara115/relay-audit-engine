"""audit_doc_id: one audit per prospect per batch.

Regression coverage for a real bug: create_audit used to mint a fresh random
document id on every call, so re-auditing a prospect added a second row to
its batch's call list instead of replacing the first. The fix keys the audit
document deterministically off (batch_id, prospect_id) so a re-audit
overwrites in place.
"""

from __future__ import annotations

import pytest

from app.store.firestore import audit_doc_id


def test_same_batch_and_prospect_always_gets_the_same_doc_id():
    first = audit_doc_id("prospect-1", "batch-a")
    second = audit_doc_id("prospect-1", "batch-a")
    assert first == second


def test_different_prospects_in_the_same_batch_get_different_ids():
    assert audit_doc_id("prospect-1", "batch-a") != audit_doc_id("prospect-2", "batch-a")


def test_the_same_prospect_in_different_batches_gets_different_ids():
    assert audit_doc_id("prospect-1", "batch-a") != audit_doc_id("prospect-1", "batch-b")


# ── Contacts, and what a re-crawl is allowed to overwrite ─────────────────────


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDoc:
    """Just enough Firestore to exercise the read-then-merge in set_contacts."""

    def __init__(self, data=None):
        self.data = data
        self.written = []

    def get(self):
        return _FakeSnapshot(self.data)

    def set(self, payload, merge=False):
        self.written.append(payload)
        base = dict(self.data or {}) if merge else {}
        base.update(payload)
        self.data = base


class _FakeCollection:
    def __init__(self, doc):
        self._doc = doc

    def document(self, _id):
        return self._doc


def _patch_client(monkeypatch, doc):
    from app.store import firestore as store

    class _Client:
        def collection(self, _name):
            return _FakeCollection(doc)

    monkeypatch.setattr(store, "get_client", lambda: _Client())


def test_discovery_picks_the_best_deliverable_address_as_owner_email(monkeypatch):
    from app.store import firestore as store

    doc = _FakeDoc({})
    _patch_client(monkeypatch, doc)

    store.set_contacts("p1", [
        {"email": "dave@roofs.com", "status": "valid"},
        {"email": "info@roofs.com", "status": "risky"},
    ])

    assert doc.data["owner_email"] == "dave@roofs.com"


def test_an_undeliverable_address_never_becomes_owner_email(monkeypatch):
    from app.store import firestore as store
    from google.cloud import firestore as gfirestore

    doc = _FakeDoc({})
    _patch_client(monkeypatch, doc)

    store.set_contacts("p1", [{"email": "x@dead.com", "status": "invalid"}])

    assert doc.data["owner_email"] is gfirestore.DELETE_FIELD


def test_a_re_crawl_does_not_clobber_an_address_a_person_hunted_down(monkeypatch):
    """The whole point of wrong_person: someone found the real name by hand."""
    from app.store import firestore as store

    doc = _FakeDoc({"manual_contacts": [{"email": "owner@roofs.com", "source": "manual"}]})
    _patch_client(monkeypatch, doc)

    store.set_contacts("p1", [{"email": "info@roofs.com", "status": "valid"}])

    assert doc.data["manual_contacts"][0]["email"] == "owner@roofs.com"
    assert doc.data["owner_email"] == "owner@roofs.com"


def test_a_re_crawl_still_refreshes_the_discovered_list(monkeypatch):
    """Manual is preserved, discovered is replaced: an address that came off
    the site should come out of the record."""
    from app.store import firestore as store

    doc = _FakeDoc({"contacts": [{"email": "gone@roofs.com", "status": "valid"}],
                    "manual_contacts": [{"email": "owner@roofs.com"}]})
    _patch_client(monkeypatch, doc)

    store.set_contacts("p1", [{"email": "new@roofs.com", "status": "valid"}])

    assert [c["email"] for c in doc.data["contacts"]] == ["new@roofs.com"]


def test_the_newest_manual_correction_wins(monkeypatch):
    from app.store import firestore as store

    doc = _FakeDoc({"manual_contacts": [{"email": "first@roofs.com"}]})
    _patch_client(monkeypatch, doc)

    store.add_manual_contact("p1", "Second@Roofs.com", note="from the receptionist")

    assert doc.data["owner_email"] == "second@roofs.com"
    assert [c["email"] for c in doc.data["manual_contacts"]] == [
        "second@roofs.com", "first@roofs.com",
    ]


def test_the_same_manual_address_twice_is_not_duplicated(monkeypatch):
    from app.store import firestore as store

    doc = _FakeDoc({"manual_contacts": [{"email": "owner@roofs.com"}]})
    _patch_client(monkeypatch, doc)

    store.add_manual_contact("p1", "owner@roofs.com")

    assert len(doc.data["manual_contacts"]) == 1


def test_a_junk_manual_address_is_refused_rather_than_stored(monkeypatch):
    from app.store import firestore as store

    doc = _FakeDoc({})
    _patch_client(monkeypatch, doc)

    for bad in ("not-an-email", "noreply@roofs.com", ""):
        with pytest.raises(ValueError):
            store.add_manual_contact("p1", bad)
