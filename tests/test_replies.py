"""Reply ingestion end to end, with Gmail and the model stubbed.

This is the module that writes permanent suppressions, so the cases that matter
are the ones where it must not: a model fault, a low confidence guess, an
address we never held.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import outreach, replies as replies_mod
from app.agents.classifier import Classification
from app.outreach import INTERESTED, NOT_INTERESTED, OTHER, OUT_OF_OFFICE, WRONG_PERSON
from app.replies import addresses_for, reply_rate, scan
from app.tools.gmail import Reply

DAY0 = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)


def reply(sender="dave@roofs.com", body="Sure, call me Thursday.", mid="m1", when=None):
    return Reply(message_id=mid, thread_id="t1", from_email=sender,
                 subject="Re: a quick thought", received_at=when or DAY0 + timedelta(days=1),
                 excerpt=body)


@pytest.fixture
def ledger(monkeypatch):
    """A single active sequence with one touch sent, and a recording store."""
    seq = outreach.advance(outreach.open_sequence("p1", audit_id="a1", now=DAY0), sent_at=DAY0)
    state = {
        "sequence": seq.to_dict(),
        "replies": [],
        "suppressions": [],
        "saved": [],
        "marked": [],
    }

    monkeypatch.setattr(replies_mod.store, "sequences_by_status",
                        lambda status, limit=200: [state["sequence"]]
                        if status == outreach.ACTIVE else [])
    monkeypatch.setattr(replies_mod.store, "get_prospect", lambda pid: {
        "business_name": "Whitaker Roofing", "domain": "roofs.com",
        "contacts": [{"email": "dave@roofs.com", "status": "valid"}],
    })
    monkeypatch.setattr(replies_mod.store, "get_sequence", lambda pid: state["sequence"])
    monkeypatch.setattr(replies_mod.store, "replies_for", lambda pid: state["replies"])
    monkeypatch.setattr(replies_mod.store, "add_reply",
                        lambda pid, row: state["replies"].append(row) or "r1")
    monkeypatch.setattr(replies_mod.store, "add_suppression",
                        lambda t, v, r: state["suppressions"].append((t, v)) or "s1")
    monkeypatch.setattr(replies_mod.store, "mark_suppressed",
                        lambda pid, reason: state["marked"].append(pid))

    def save(seq_obj):
        state["saved"].append(seq_obj)
        state["sequence"] = seq_obj.to_dict()

    monkeypatch.setattr(replies_mod.store, "save_sequence", save)
    return state


def run_scan(monkeypatch, *, inbox, classification):
    monkeypatch.setattr(replies_mod, "fetch_replies",
                        lambda addrs, since, service=None: list(inbox))

    async def fake_classify(body, subject=""):
        return classification

    monkeypatch.setattr(replies_mod, "classify", fake_classify)
    return asyncio.run(scan())


# ── Which addresses get searched ──────────────────────────────────────────────


def test_a_manual_address_is_searched_before_a_discovered_one():
    got = addresses_for({
        "contacts": [{"email": "info@roofs.com", "status": "valid"}],
        "manual_contacts": [{"email": "owner@roofs.com"}],
    })
    assert got == ["owner@roofs.com", "info@roofs.com"]


def test_an_address_we_know_is_dead_is_not_searched():
    assert addresses_for({"contacts": [{"email": "x@roofs.com", "status": "invalid"}]}) == []


def test_a_prospect_with_no_address_is_skipped_entirely(monkeypatch, ledger):
    monkeypatch.setattr(replies_mod.store, "get_prospect", lambda pid: {"business_name": "Peak"})
    called: list = []
    monkeypatch.setattr(replies_mod, "fetch_replies",
                        lambda *a, **k: called.append(1) or [])
    result = asyncio.run(scan())
    assert called == []
    assert result.found == 0


# ── What a reply does ─────────────────────────────────────────────────────────


def test_an_interested_reply_closes_without_suppressing(monkeypatch, ledger):
    result = run_scan(monkeypatch, inbox=[reply()],
                      classification=Classification(INTERESTED, 0.95, "wants a call"))
    assert result.found == 1
    assert result.outcomes[0].closed is True
    assert result.outcomes[0].suppressed is False
    assert ledger["suppressions"] == []


def test_a_no_suppresses_on_every_identifier_we_hold(monkeypatch, ledger):
    result = run_scan(monkeypatch, inbox=[reply(body="Take me off your list.")],
                      classification=Classification(NOT_INTERESTED, 0.97, "explicit no"))
    assert result.outcomes[0].suppressed is True
    kinds = dict(ledger["suppressions"])
    assert kinds["email"] == "dave@roofs.com"
    assert kinds["place_id"] == "p1"
    assert kinds["domain"] == "roofs.com"
    assert ledger["marked"] == ["p1"]


def test_a_low_confidence_no_parks_instead_of_suppressing(monkeypatch, ledger):
    """The failure that would be unrecoverable. A guess must not end a prospect."""
    result = run_scan(monkeypatch, inbox=[reply()],
                      classification=Classification(NOT_INTERESTED, 0.2, "unsure"))
    assert result.outcomes[0].intent == OTHER
    assert result.outcomes[0].suppressed is False
    assert result.outcomes[0].parked is True
    assert ledger["suppressions"] == []


def test_a_model_fault_parks_instead_of_suppressing(monkeypatch, ledger):
    result = run_scan(monkeypatch, inbox=[reply()],
                      classification=Classification(OTHER, 0.0, "", error="ServerError"))
    assert result.outcomes[0].parked is True
    assert ledger["suppressions"] == []


def test_an_out_of_office_does_not_burn_the_touch(monkeypatch, ledger):
    run_scan(monkeypatch, inbox=[reply(body="I am away until the 20th.")],
             classification=Classification(OUT_OF_OFFICE, 1.0, "auto", auto=True))
    assert ledger["saved"][0].touch_count == 0     # rewound from one
    assert ledger["saved"][0].status == outreach.ACTIVE


def test_a_forward_parks_for_a_new_contact(monkeypatch, ledger):
    result = run_scan(monkeypatch, inbox=[reply(body="Passing this to our GM.")],
                      classification=Classification(WRONG_PERSON, 0.9, "forwarded"))
    assert result.outcomes[0].parked is True
    assert ledger["suppressions"] == []


def test_the_model_sees_the_reply_and_the_ledger_records_both(monkeypatch, ledger):
    run_scan(monkeypatch, inbox=[reply()],
             classification=Classification(NOT_INTERESTED, 0.3, "unsure"))
    stored = ledger["replies"][0]
    assert stored["intent"] == OTHER                      # what was acted on
    assert stored["classification"]["intent"] == NOT_INTERESTED   # what was guessed
    assert stored["excerpt"] == "Sure, call me Thursday."


def test_a_reply_already_recorded_is_not_acted_on_twice(monkeypatch, ledger):
    ledger["replies"].append({"message_id": "m1"})
    result = run_scan(monkeypatch, inbox=[reply(mid="m1")],
                      classification=Classification(NOT_INTERESTED, 0.99, "no"))
    assert result.found == 0
    assert result.skipped_seen == 1
    assert ledger["suppressions"] == []


def test_two_replies_land_in_the_order_they_were_written(monkeypatch, ledger):
    later = reply(mid="m2", body="Actually, forget it.", when=DAY0 + timedelta(days=3))
    earlier = reply(mid="m1", body="Maybe.", when=DAY0 + timedelta(days=1))
    result = run_scan(monkeypatch, inbox=[later, earlier],
                      classification=Classification(INTERESTED, 0.9, "x"))
    assert [o.reply.message_id for o in result.outcomes] == ["m1", "m2"]


def test_a_missing_mailbox_is_a_skipped_scan_not_a_crash(monkeypatch, ledger):
    from app.tools.gmail import GmailUnavailable

    def boom(*a, **k):
        raise GmailUnavailable("no token")

    monkeypatch.setattr(replies_mod, "fetch_replies", boom)
    result = asyncio.run(scan())
    assert result.error == "no token"
    assert result.found == 0


# ── The section 7 number ──────────────────────────────────────────────────────


def test_the_rate_is_absent_rather_than_zero_before_anything_is_sent(monkeypatch):
    monkeypatch.setattr(replies_mod.store, "all_sequences", lambda: [])
    assert reply_rate()["rate"] is None


def test_the_rate_counts_messages_not_prospects(monkeypatch):
    monkeypatch.setattr(replies_mod.store, "all_sequences",
                        lambda: [{"prospect_id": "p1"}, {"prospect_id": "p2"}])
    monkeypatch.setattr(replies_mod.store, "touches_for",
                        lambda pid: [{}, {}] if pid == "p1" else [{}])
    monkeypatch.setattr(replies_mod.store, "replies_for",
                        lambda pid: [{"intent": INTERESTED}] if pid == "p1" else [])
    monkeypatch.setattr(replies_mod.store, "get_audit", lambda a: None)

    got = reply_rate()
    assert got["sent"] == 3
    assert got["replied"] == 1
    assert got["by_intent"] == {INTERESTED: 1}
    assert got["meets_threshold"] is False
