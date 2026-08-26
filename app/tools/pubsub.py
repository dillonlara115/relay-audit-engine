"""Pub/Sub publishing. One message per prospect, fanned out to the workers.

The message is deliberately thin: a batch id and a prospect id. Everything else
is read from Firestore by whichever worker picks it up, because a message that
carries state goes stale the moment it is retried, and these messages are
retried by design.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from functools import lru_cache
from typing import Iterable, Sequence

from google.cloud import pubsub_v1

from app.config import get_config

AUDIT_EVENT = "run_audit"
JOB_EVENT = "run_job"


@lru_cache(maxsize=1)
def _publisher() -> pubsub_v1.PublisherClient:
    # Batching lets a hundred prospect messages leave in a handful of requests.
    settings = pubsub_v1.types.BatchSettings(max_messages=100, max_latency=0.5)
    return pubsub_v1.PublisherClient(batch_settings=settings)


def topic_path(topic: str | None = None) -> str:
    cfg = get_config()
    cfg.require("project")
    return _publisher().topic_path(cfg.project, topic or cfg.pubsub_audit_topic)


def publish_audit(batch_id: str, prospect_id: str, *, topic: str | None = None) -> Future:
    payload = json.dumps(
        {"event": AUDIT_EVENT, "batch_id": batch_id, "prospect_id": prospect_id}
    ).encode()
    return _publisher().publish(
        topic_path(topic),
        payload,
        # Attributes are visible in the console without decoding the body, which
        # matters when you are staring at a dead letter queue at midnight.
        event=AUDIT_EVENT,
        batch_id=batch_id,
        prospect_id=prospect_id,
    )


def publish_batch(batch_id: str, prospect_ids: Iterable[str], *, topic: str | None = None) -> int:
    """Publish one message per prospect and wait for every ack.

    Waiting matters: a fire and forget publish that fails leaves a prospect that
    never gets audited and never appears in the dead letter queue either, so the
    batch just quietly comes up short.
    """
    futures: list[tuple[str, Future]] = [
        (pid, publish_audit(batch_id, pid, topic=topic)) for pid in prospect_ids
    ]
    published = 0
    errors: list[str] = []
    for prospect_id, future in futures:
        try:
            future.result(timeout=60)
            published += 1
        except Exception as exc:  # noqa: BLE001 - report, do not lose the rest
            errors.append(f"{prospect_id}: {type(exc).__name__}: {exc}")
    if errors:
        raise RuntimeError(f"{len(errors)} of {len(futures)} publishes failed: {errors[:3]}")
    return published


def parse_push(envelope: dict) -> dict:
    """Decode a Pub/Sub push envelope into the message body.

    Raises ValueError on anything malformed. Pub/Sub acks only on 102, 200,
    201, 202 and 204, so the worker answers 400 and the message is redelivered
    and then dead lettered. That is deliberate: a message we cannot parse will
    not parse on the fifth attempt either, but a malformed message that vanishes
    silently is worse than one sitting visibly in a dead letter queue.
    """
    import base64

    message = (envelope or {}).get("message")
    if not isinstance(message, dict):
        raise ValueError("envelope has no message")

    data = message.get("data")
    if not data:
        attributes = message.get("attributes") or {}
        if attributes.get("job_id"):
            return {"event": JOB_EVENT, "job_id": attributes["job_id"],
                    "kind": attributes.get("kind"),
                    "message_id": message.get("messageId")}
        if attributes.get("batch_id") and attributes.get("prospect_id"):
            return {
                "event": attributes.get("event", AUDIT_EVENT),
                "batch_id": attributes["batch_id"],
                "prospect_id": attributes["prospect_id"],
                "message_id": message.get("messageId"),
                "delivery_attempt": envelope.get("deliveryAttempt"),
            }
        raise ValueError("message has no data and no usable attributes")

    try:
        body = json.loads(base64.b64decode(data).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"undecodable message data: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("message data was not an object")
    if body.get("event") == JOB_EVENT:
        if not body.get("job_id"):
            raise ValueError("job message is missing job_id")
    elif not body.get("batch_id") or not body.get("prospect_id"):
        raise ValueError("message is missing batch_id or prospect_id")

    body["message_id"] = message.get("messageId")
    body["delivery_attempt"] = envelope.get("deliveryAttempt")
    return body


def publish_job(job_id: str, kind: str, *, topic: str | None = None) -> str:
    """Queue a long running operator job. Same transport as an audit, because
    the retry, dead letter and at-least-once behaviour are the same needs."""
    cfg = get_config()
    payload = json.dumps({"event": JOB_EVENT, "job_id": job_id, "kind": kind}).encode()
    future = _publisher().publish(
        topic_path(topic or cfg.pubsub_job_topic),
        payload,
        event=JOB_EVENT, job_id=job_id, kind=kind,
    )
    return future.result(timeout=60)
