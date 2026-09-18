"""Quo webhook deliveries: verified, deduplicated, and turned into ledger rows."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import outreach, quo_events
from app.tools import quo

KEY = "whsec_" + "c2VjcmV0LWtleS1mb3ItdGVzdHM="


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def ledger(monkeypatch):
    """A fake store: prospects by contact id and phone, and everything written."""
    from app.agents import classifier

    written = {"touches": [], "replies": [], "sequences": [], "suppressions": [], "updates": [], "claimed": []}
    prospects = {"CT1": {"place_id": "p1", "business_name": "Apex", "domain": "apex.com", "phone_e164": "+19702241200"}}
    monkeypatch.setattr(quo_events.store, "prospect_by_quo_contact", lambda cid: prospects.get(cid))
    monkeypatch.setattr(quo_events.store, "prospect_by_phone",
                        lambda ph: next((p for p in prospects.values() if p["phone_e164"] == ph), None))

    def claim(eid):
        if eid in written["claimed"]:
            return False
        written["claimed"].append(eid)
        return True
    monkeypatch.setattr(quo_events.store, "claim_event", claim)
    monkeypatch.setattr(quo_events.store, "get_sequence", lambda pid: None)
    monkeypatch.setattr(quo_events.store, "save_sequence", lambda seq: written["sequences"].append(seq))
    monkeypatch.setattr(quo_events.store, "add_reply", lambda pid, r: written["replies"].append((pid, r)) or "r1")
    monkeypatch.setattr(quo_events.store, "add_touch", lambda pid, t: written["touches"].append((pid, t)) or "t1")
    monkeypatch.setattr(quo_events.store, "add_suppression",
                        lambda kind, value, reason: written["suppressions"].append((kind, value, reason)) or "s1")
    monkeypatch.setattr(quo_events.store, "mark_suppressed", lambda pid, reason: None)
    monkeypatch.setattr(quo_events.store, "touch_by_resource",
                        lambda pid, rid: ("t1", {"resource_id": rid}) if rid == "AC2" else None)
    monkeypatch.setattr(quo_events.store, "update_touch", lambda pid, tid, f: written["updates"].append((pid, tid, f)))

    async def fake_classify(body, *, subject=""):
        intent = outreach.INTERESTED if "call me" in body.lower() else outreach.OTHER
        return classifier.Classification(intent, 0.9, "stub")
    monkeypatch.setattr(quo_events, "classify", fake_classify)
    return written


def text_in(text, *, event_id="EV1", contact="CT1", phone="+19702241200"):
    return {"id": event_id, "type": "message.received", "data": {
        "resource": {"id": "AC1", "direction": "incoming", "text": text, "status": "received"},
        "context": {"senderIdentifier": phone, "contacts": {"ids": [contact] if contact else []}},
        "links": {"quo": "https://my.quo.com/c/1"}}}


def test_a_stop_suppresses_before_anything_else(ledger):
    out = run(quo_events.handle(text_in("STOP")))
    assert out.action == "suppressed" and out.prospect_id == "p1"
    kinds = {(k, v) for k, v, _ in ledger["suppressions"]}
    assert ("phone", "+19702241200") in kinds and ("place_id", "p1") in kinds and ("domain", "apex.com") in kinds
    assert ledger["sequences"][0].status == outreach.CLOSED
    assert ledger["replies"][0][1]["intent"] == outreach.NOT_INTERESTED


def test_an_inbound_text_is_classified_recorded_and_parks_the_sequence(ledger):
    out = run(quo_events.handle(text_in("Sure, call me Thursday")))
    assert out.action == "reply" and out.intent == outreach.INTERESTED
    pid, reply = ledger["replies"][0]
    assert pid == "p1" and reply["channel"] == "sms" and reply["from_phone"] == "+19702241200"
    assert reply["excerpt"] == "Sure, call me Thursday" and reply["link"] == "https://my.quo.com/c/1"
    assert ledger["sequences"][0].status in (outreach.WAITING, outreach.CLOSED)
    assert ledger["suppressions"] == []


def test_a_text_from_nobody_we_audited_is_ignored(ledger):
    out = run(quo_events.handle(text_in("hello?", contact=None, phone="+15550001111")))
    assert out.action == "unknown" and ledger["replies"] == []


def test_a_retried_delivery_is_not_recorded_twice(ledger):
    run(quo_events.handle(text_in("Sure, call me Thursday", event_id="EV9")))
    out = run(quo_events.handle(text_in("Sure, call me Thursday", event_id="EV9")))
    assert out.action == "duplicate" and len(ledger["replies"]) == 1


def test_a_finished_call_is_a_touch_and_its_summary_attaches(ledger):
    call = {"id": "EV2", "type": "call.completed", "data": {
        "resource": {"id": "AC2", "direction": "outgoing", "status": "completed", "duration": 184,
                     "answeredAt": "2026-09-18T15:00:00Z"},
        "context": {"recipientIdentifiers": ["+19702241200"], "contacts": {"ids": ["CT1"]}},
        "links": {"quo": "https://my.quo.com/c/2"}}}
    out = run(quo_events.handle(call))
    assert out.action == "touch"
    pid, touch = ledger["touches"][0]
    assert touch["channel"] == "call" and touch["duration"] == 184 and touch["answered"] and touch["resource_id"] == "AC2"
    assert "ordinal" not in touch, "a call is not one of the four emails"
    summary = {"id": "EV3", "type": "call.summary.completed", "data": {
        "resource": {"callId": "AC2", "summary": ["Owner interested."], "nextSteps": ["Send the report."]},
        "context": {"recipientIdentifiers": ["+19702241200"], "contacts": {"ids": ["CT1"]}}}}
    out = run(quo_events.handle(summary))
    assert out.action == "summary"
    assert ledger["updates"][0] == ("p1", "t1", {"summary": ["Owner interested."], "next_steps": ["Send the report."]})


def test_status_and_task_events_are_ignored_without_a_claim(ledger):
    for t in ("message.delivered", "task.created", "contact.updated"):
        out = run(quo_events.handle({"id": "EVx", "type": t, "data": {}}))
        assert out.action == "ignored"
    assert ledger["claimed"] == []


# ── The route ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def hook(monkeypatch):
    import app.worker as worker
    from app.config import Config

    monkeypatch.setattr(worker, "get_config", lambda: Config(quo_webhook_key=KEY))
    return TestClient(worker.app, raise_server_exceptions=False)


def post(client, payload, *, key=KEY, headers=None):
    raw = json.dumps(payload).encode()
    h = headers if headers is not None else quo.sign(raw, key, webhook_id="w1", timestamp="1700000000")
    return client.post("/quo/webhook", content=raw, headers={**h, "content-type": "application/json"})


def test_the_webhook_refuses_unsigned_and_mis_signed_deliveries(hook):
    assert post(hook, {"type": "task.created"}, headers={}).status_code == 401
    assert post(hook, {"type": "task.created"}, key="whsec_b3RoZXI=").status_code == 401


def test_the_webhook_acknowledges_a_signed_delivery_and_reports_the_action(hook, monkeypatch):
    async def fake(payload):
        return quo_events.Outcome("ignored", detail=payload["type"])
    monkeypatch.setattr(quo_events, "handle", fake)
    r = post(hook, {"id": "EV1", "type": "task.created", "data": {}})
    assert r.status_code == 200 and r.json() == {"action": "ignored"}


def test_a_handler_error_is_logged_and_still_acknowledged(hook, monkeypatch):
    async def boom(payload):
        raise RuntimeError("firestore down")
    monkeypatch.setattr(quo_events, "handle", boom)
    r = post(hook, {"id": "EV1", "type": "message.received", "data": {}})
    assert r.status_code == 200, "a 5xx would make Quo retry forever"


def test_the_webhook_is_off_when_no_key_is_configured(monkeypatch):
    import app.worker as worker
    from app.config import Config

    monkeypatch.setattr(worker, "get_config", lambda: Config(quo_webhook_key=""))
    client = TestClient(worker.app, raise_server_exceptions=False)
    raw = b"{}"
    r = client.post("/quo/webhook", content=raw, headers=quo.sign(raw, KEY, webhook_id="w", timestamp="1"))
    assert r.status_code == 401
