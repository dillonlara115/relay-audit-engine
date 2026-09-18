"""The Quo module. No network: httpx is given a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest

from app.tools import quo


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("QUO_API_KEY", "key-123")
    monkeypatch.setenv("QUO_FROM", "+15732569991")
    from app import config
    config.get_config.cache_clear() if hasattr(config.get_config, "cache_clear") else None
    yield
    config.get_config.cache_clear() if hasattr(config.get_config, "cache_clear") else None


def transport(handler):
    calls = []

    def _handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(_handle)), calls


def test_auth_header_is_the_bare_key_and_the_version_is_pinned():
    client, calls = transport(lambda r: httpx.Response(200, json={"data": []}))
    quo.find_contact("ChIJabc", client=client)
    assert calls[0].headers["authorization"] == "key-123"
    assert not calls[0].headers["authorization"].startswith("Bearer")
    assert calls[0].headers["quo-api-version"] == quo.API_VERSION


def test_no_key_means_no_request(monkeypatch):
    monkeypatch.setenv("QUO_API_KEY", "")
    from app import config
    if hasattr(config.get_config, "cache_clear"):
        config.get_config.cache_clear()
    with pytest.raises(quo.QuoUnavailable) as exc:
        quo.find_contact("x", client=transport(lambda r: httpx.Response(200))[0])
    assert "QUO_API_KEY" in str(exc.value)


def test_contact_payload_names_the_business_and_carries_the_report():
    p = quo.contact_payload({"place_id": "ChIJabc", "business_name": "Apex Roofing",
                             "gbp_phone": "(970) 224-1200", "owner_email": "m@apex.com",
                             "city": "Fort Collins"},
                            report_url="https://r/abc", console_url="https://c/p")
    assert p["externalId"] == "ChIJabc" and p["source"] == "relay-audit-engine"
    df = p["defaultFields"]
    assert df["firstName"] == "Apex Roofing" and df["company"] == "Apex Roofing"
    assert df["phoneNumbers"] == [{"name": "business", "value": "+19702241200"}]
    assert df["emails"] == [{"name": "owner", "value": "m@apex.com"}]
    assert {"name": "Relay report", "value": "https://r/abc"} in p["customFields"]


def test_contact_payload_uses_the_owner_name_when_known():
    df = quo.contact_payload({"place_id": "x", "business_name": "Apex", "owner_name": "Dave Whitaker"})["defaultFields"]
    assert (df["firstName"], df["lastName"], df["role"], df["company"]) == ("Dave", "Whitaker", "Owner", "Apex")


def test_ensure_contact_reuses_an_existing_one_and_creates_once():
    seen = []

    def handler(r):
        seen.append((r.method, r.url.path, dict(r.url.params)))
        if r.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(201, json={"data": {"id": "CT1"}})

    client, _ = transport(handler)
    assert quo.ensure_contact({"place_id": "ChIJabc", "business_name": "Apex"}, client=client) == "CT1"
    assert seen[0][0] == "GET" and seen[0][2]["externalIds"] == "ChIJabc"
    assert seen[1][0] == "POST" and seen[1][1] == "/v1/contacts"

    client, calls = transport(lambda r: httpx.Response(200, json={"data": [{"id": "CT9"}]}))
    assert quo.ensure_contact({"place_id": "ChIJabc", "business_name": "Apex"}, client=client) == "CT9"
    assert [c.method for c in calls] == ["GET"], "found, so no create"


def test_send_text_sends_one_message_to_one_number():
    client, calls = transport(lambda r: httpx.Response(202, json={
        "data": {"id": "AC1", "conversationId": "CN1", "status": "queued"}}))
    out = quo.send_text(to="(970) 224-1200", content="Hi  there,\nthis is Dillon.", client=client)
    assert out == quo.SentText("AC1", "CN1", "queued")
    body = json.loads(calls[0].content)
    assert body == {"content": "Hi there, this is Dillon.", "from": "+15732569991", "to": ["+19702241200"]}
    assert calls[0].url.path == "/v1/messages"


@pytest.mark.parametrize("to", ["", "555", "a@b.com"])
def test_send_text_refuses_anything_but_a_real_number(to):
    with pytest.raises(ValueError):
        quo.send_text(to=to, content="x", client=transport(lambda r: httpx.Response(202))[0])


def test_send_text_caps_the_length():
    with pytest.raises(ValueError):
        quo.send_text(to="+19702241200", content="x" * (quo.TEXT_CAP + 1),
                      client=transport(lambda r: httpx.Response(202))[0])


def test_quo_errors_are_explained_in_the_operators_words():
    client, _ = transport(lambda r: httpx.Response(400, json={"code": "0206400", "message": "A2P Registration Not Approved"}))
    with pytest.raises(quo.QuoUnavailable) as exc:
        quo.send_text(to="+19702241200", content="hi", client=client)
    assert "not approved for A2P 10DLC" in str(exc.value)


# ── Webhooks ──────────────────────────────────────────────────────────────────

KEY = "whsec_" + "c2VjcmV0LWtleS1mb3ItdGVzdHM="   # base64("secret-key-for-tests")


def test_signature_round_trips_and_rejects_tampering():
    body = b'{"type":"message.received","data":{}}'
    headers = quo.sign(body, KEY, webhook_id="msg_1", timestamp="1700000000")
    assert quo.verify_signature(headers, body, KEY)
    assert not quo.verify_signature(headers, body + b" ", KEY), "raw bytes matter"
    assert not quo.verify_signature(headers, body, "whsec_" + "b3RoZXI=")
    assert not quo.verify_signature({}, body, KEY)
    assert not quo.verify_signature(headers, body, "")


def test_signature_accepts_any_of_several_v1_entries():
    body = b"{}"
    good = quo.sign(body, KEY, webhook_id="w", timestamp="1")["webhook-signature"]
    headers = {"Webhook-Id": "w", "Webhook-Timestamp": "1", "Webhook-Signature": "v1,AAAA " + good}
    assert quo.verify_signature(headers, body, KEY)


def test_parse_inbound_text():
    ev = quo.parse_event({"id": "EV1", "type": "message.received", "data": {
        "resource": {"id": "AC1", "direction": "incoming", "text": "Sure,  call me Thursday", "status": "received"},
        "context": {"senderIdentifier": "+19702241200", "recipientIdentifiers": ["+15732569991"],
                    "contacts": {"ids": ["CT1"]}},
        "links": {"quo": "https://my.quo.com/x"}}})
    assert ev.kind == "text_in" and ev.phone == "+19702241200" and ev.text == "Sure, call me Thursday"
    assert ev.contact_ids == ("CT1",) and ev.link == "https://my.quo.com/x" and ev.resource_id == "AC1"


def test_parse_completed_call_and_summary():
    ev = quo.parse_event({"id": "EV2", "type": "call.completed", "data": {
        "resource": {"id": "AC2", "direction": "outgoing", "status": "completed", "duration": 184,
                     "answeredAt": "2026-09-18T15:00:00Z"},
        "context": {"recipientIdentifiers": ["+19702241200"], "contacts": {"ids": []}}}})
    assert ev.kind == "call_done" and ev.answered and ev.duration == 184 and ev.phone == "+19702241200"
    sm = quo.parse_event({"id": "EV3", "type": "call.summary.completed", "data": {
        "resource": {"callId": "AC2", "summary": ["Owner interested", "Send report"], "nextSteps": ["Email Thursday"]},
        "context": {"recipientIdentifiers": ["+19702241200"]}}})
    assert sm.kind == "call_summary" and sm.resource_id == "AC2"
    assert sm.summary == ("Owner interested", "Send report") and sm.next_steps == ("Email Thursday",)


def test_unknown_events_are_other_not_errors():
    assert quo.parse_event({"type": "task.created", "data": {}}).kind == "other"


@pytest.mark.parametrize("text,stop", [("STOP", True), ("stop.", True), ("Unsubscribe", True),
                                       ("Stop calling", False), ("Sure", False)])
def test_stop_words(text, stop):
    assert quo.is_stop(text) is stop
