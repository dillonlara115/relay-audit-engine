"""Push envelope parsing and ack semantics.

Pub/Sub acks on 102, 200, 201, 202 and 204 only. Everything else is a nack.
Getting that backwards either loses prospects silently or loops them forever,
and neither shows up until a batch is running unattended, so it is pinned here.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.tools.pubsub import AUDIT_EVENT, parse_push

# Statuses Pub/Sub treats as a successful delivery.
ACK_STATUSES = {102, 200, 201, 202, 204}


def envelope(body=None, *, attributes=None, message_id="m1", delivery_attempt=None):
    message = {"messageId": message_id}
    if body is not None:
        message["data"] = base64.b64encode(json.dumps(body).encode()).decode()
    if attributes:
        message["attributes"] = attributes
    out = {"message": message, "subscription": "projects/p/subscriptions/s"}
    if delivery_attempt is not None:
        out["deliveryAttempt"] = delivery_attempt
    return out


# ── Parsing ───────────────────────────────────────────────────────────────────


def test_a_normal_message_parses():
    body = parse_push(envelope({"event": AUDIT_EVENT, "batch_id": "b1", "prospect_id": "p1"}))
    assert body["batch_id"] == "b1"
    assert body["prospect_id"] == "p1"
    assert body["message_id"] == "m1"


def test_delivery_attempt_is_carried_through():
    body = parse_push(envelope({"batch_id": "b1", "prospect_id": "p1"}, delivery_attempt=3))
    assert body["delivery_attempt"] == 3


def test_attributes_are_used_when_the_body_is_empty():
    """The publisher sets both, so a body that fails to encode is recoverable."""
    body = parse_push(envelope(attributes={"batch_id": "b1", "prospect_id": "p1"}))
    assert body["batch_id"] == "b1" and body["prospect_id"] == "p1"


@pytest.mark.parametrize(
    "bad, why",
    [
        ({}, "no message"),
        ({"message": "not a dict"}, "message is not an object"),
        ({"message": {"messageId": "m1"}}, "no data and no attributes"),
        (envelope({"batch_id": "b1"}), "no prospect_id"),
        (envelope({"prospect_id": "p1"}), "no batch_id"),
        (envelope([1, 2, 3]), "data is not an object"),
        ({"message": {"data": "!!!not base64!!!"}}, "undecodable"),
    ],
)
def test_malformed_envelopes_raise(bad, why):
    with pytest.raises(ValueError):
        parse_push(bad)


# ── Ack semantics ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch):
    """Pins the worker's config instead of inheriting .env: the local .env now
    carries a real WORKER_SHARED_SECRET, and these tests are about ack
    semantics, not the token gate (which has its own tests below)."""
    from app.config import Config

    monkeypatch.setattr("app.worker._defs", lambda: [])
    monkeypatch.setattr("app.worker.get_config", lambda: Config(worker_shared_secret=""))
    from app.worker import app

    return TestClient(app, raise_server_exceptions=False)


def push(client, body, **kw):
    return client.post("/pubsub/audit", json=envelope(body, **kw))


def test_health_reports_the_build(client):
    body = client.get("/health").json()
    assert body["ok"] is True and body["worker"]


def test_a_finished_task_is_acked(client, monkeypatch):
    from app.tasks import TaskOutcome

    async def done(batch_id, prospect_id, **kw):
        return TaskOutcome(True, "audited", prospect_id, batch_id, audit_id="a1", total=42)

    monkeypatch.setattr("app.worker.run_audit_task", done)
    r = push(client, {"batch_id": "b1", "prospect_id": "p1"})
    assert r.status_code in ACK_STATUSES
    assert r.headers["x-relay-reason"] == "audited"


def test_a_contended_task_is_nacked_for_redelivery(client, monkeypatch):
    """Someone else holds it. Coming back later is correct; acking loses it."""
    from app.tasks import TaskOutcome

    async def busy(batch_id, prospect_id, **kw):
        return TaskOutcome(False, "host example.com busy", prospect_id, batch_id)

    monkeypatch.setattr("app.worker.run_audit_task", busy)
    r = push(client, {"batch_id": "b1", "prospect_id": "p1"})
    assert r.status_code not in ACK_STATUSES
    assert r.status_code == 409


def test_a_crashed_worker_never_acks(client, monkeypatch):
    """The dangerous case. A 200 here would drop the prospect silently."""
    async def explode(batch_id, prospect_id, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.worker.run_audit_task", explode)
    r = push(client, {"batch_id": "b1", "prospect_id": "p1"})
    assert r.status_code not in ACK_STATUSES
    assert r.status_code == 500


def test_a_malformed_message_is_nacked_toward_the_dead_letter_queue(client):
    r = client.post("/pubsub/audit", json={"garbage": True})
    assert r.status_code == 400
    assert r.status_code not in ACK_STATUSES


def test_unparseable_json_body_is_rejected(client):
    r = client.post("/pubsub/audit", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400


# ── The shared secret ─────────────────────────────────────────────────────────


def test_the_push_token_is_enforced_when_configured(client, monkeypatch):
    from app.config import Config

    monkeypatch.setattr("app.worker.get_config", lambda: Config(worker_shared_secret="s3cret"))
    assert client.post("/pubsub/audit", json=envelope({"batch_id": "b", "prospect_id": "p"})
                       ).status_code == 401
    assert client.post("/pubsub/audit?token=wrong",
                       json=envelope({"batch_id": "b", "prospect_id": "p"})).status_code == 401


def test_no_secret_configured_means_iam_is_the_only_gate(client, monkeypatch):
    from app.config import Config

    monkeypatch.setattr("app.worker.get_config", lambda: Config(worker_shared_secret=""))
    r = client.post("/pubsub/audit", json={"garbage": True})
    assert r.status_code == 400, "reached the parser rather than being rejected at the door"
