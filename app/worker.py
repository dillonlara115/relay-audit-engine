"""The audit worker. Receives Pub/Sub push deliveries, one prospect per message.

Ack semantics are the whole contract with Pub/Sub, and they are easy to get
backwards. Pub/Sub acks on 102, 200, 201, 202 and 204 only. Every other status,
and every timeout, is a nack that schedules a redelivery and counts toward the
dead letter threshold.

So:
  204  we finished, or there is provably nothing to do          -> ack
  409  someone else holds it, or the host is busy               -> nack, retry
  400  the message is malformed                                 -> nack, then DLQ
  500  we broke                                                 -> nack, retry

Nothing here returns 200 for work that did not happen. Losing a prospect is
silent; a redelivery is not.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request, Response

from app.config import get_config
from app.leases import worker_id
from app.store import firestore as store
from app.tasks import run_audit_task
from app.tools.pubsub import parse_push

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay.worker")

BUILD_SHA = os.getenv("BUILD_SHA", "dev")

app = FastAPI(title="relay-audit-worker")

# One worker identity per process, so leases can be attributed and reclaimed.
WORKER = worker_id()

# Loaded once. Check definitions change by document edit, and a worker instance
# is short lived enough that picking them up on cold start is soon enough.
_definitions: list | None = None


def _defs() -> list:
    global _definitions
    if _definitions is None:
        _definitions = store.all_check_defs()
    return _definitions


@app.get("/health")
@app.get("/healthz")
def health() -> dict:
    return {"ok": True, "build": BUILD_SHA, "worker": WORKER}


def _authorized(request: Request) -> bool:
    """Defence in depth behind Cloud Run IAM.

    Pub/Sub push cannot set an arbitrary header, but it can carry a token in the
    push endpoint's query string, so that is where the shared secret lives.
    """
    secret = get_config().worker_shared_secret
    if not secret:
        return True
    provided = request.query_params.get("token") or request.headers.get("x-relay-secret")
    return provided == secret


@app.post("/pubsub/audit")
async def pubsub_audit(request: Request) -> Response:
    if not _authorized(request):
        log.warning("rejected an unauthorized push")
        return Response(status_code=401)

    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001
        return Response(status_code=400)

    try:
        message = parse_push(envelope)
    except ValueError as exc:
        # Redelivered and then dead lettered, on purpose. See module docstring.
        log.error("malformed push: %s", exc)
        return Response(status_code=400)

    batch_id = message["batch_id"]
    prospect_id = message["prospect_id"]
    attempt = message.get("delivery_attempt")
    log.info("claiming %s in %s (attempt %s)", prospect_id, batch_id, attempt)

    try:
        outcome = await run_audit_task(
            batch_id, prospect_id, worker=WORKER, definitions=_defs()
        )
    except Exception as exc:  # noqa: BLE001 - never 200 on an unfinished audit
        log.exception("worker failed on %s", prospect_id)
        return Response(status_code=500, headers={"x-relay-error": type(exc).__name__})

    log.info("%s -> %s (ack=%s)", prospect_id, outcome.reason, outcome.ack)
    if outcome.ack:
        return Response(status_code=204, headers={"x-relay-reason": outcome.reason[:80]})
    # Contended rather than broken. 409 keeps it out of the error logs while
    # still telling Pub/Sub to come back.
    return Response(status_code=409, headers={"x-relay-reason": outcome.reason[:80]})
