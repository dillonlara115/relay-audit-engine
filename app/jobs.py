"""Long running operator jobs, as records rather than requests.

A sweep takes minutes and a coordinator run takes longer. Neither fits in an
HTTP request, and Cloud Run throttles CPU once a response is sent, so a
background task started in a request handler is not guaranteed to finish.

So a job is a Firestore document plus a Pub/Sub message: the request that
starts one returns immediately with a job id, a worker picks the message up,
and the browser polls the document. That reuses the retry, dead letter and
at-least-once machinery the audit fan-out already proved, and it means a job
survives the instance that started it being killed.

The claim is the same shape as an audit task's, for the same reason: Pub/Sub
delivers at least once and a sweep costs real Places quota, so a duplicate
delivery must not run it twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

from google.cloud import firestore

from app.store import firestore as store

JOBS = "jobs"

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

KIND_SWEEP = "sweep"
KIND_DISPATCH = "dispatch"
KIND_AGENT = "agent"
KIND_AUDIT = "audit"
KIND_DRAFT = "draft"

# A sweep of 120 prospects runs about five minutes. A coordinator run that
# waits on a batch can run much longer, so the lease is generous and renewed.
JOB_LEASE_SECONDS = 1800

# Keep the log bounded: a document has a 1 MiB ceiling and a chatty job would
# walk into it. The tail is what an operator reads anyway.
MAX_LOG_LINES = 400


@dataclass(frozen=True)
class JobClaim:
    granted: bool
    reason: str
    attempts: int = 0


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


def create(
    kind: str,
    params: Mapping[str, Any],
    *,
    created_by: str = "operator",
    label: str | None = None,
) -> str:
    """Record a job as queued. Publishing it is the caller's next step."""
    job_id = new_job_id()
    store.get_client().collection(JOBS).document(job_id).set(
        store._plain({
            "job_id": job_id,
            "kind": kind,
            "label": label or kind,
            "params": dict(params),
            "status": QUEUED,
            "attempts": 0,
            "log": [],
            "created_by": created_by,
            "created_at": store.utcnow(),
            "updated_at": store.utcnow(),
        })
    )
    return job_id


def claim(job_id: str, *, worker: str, ttl: int = JOB_LEASE_SECONDS) -> JobClaim:
    """Take ownership, or explain why not. Same contract as an audit claim."""
    client = store.get_client()
    ref = client.collection(JOBS).document(job_id)

    @firestore.transactional
    def run(transaction: firestore.Transaction) -> JobClaim:
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return JobClaim(False, "no such job")
        data = snapshot.to_dict() or {}
        status = data.get("status")
        attempts = int(data.get("attempts") or 0)
        now = store.utcnow()

        if status == DONE:
            return JobClaim(False, "already done", attempts)
        if status == RUNNING:
            expires = data.get("lease_expires_at")
            if expires is not None and expires.replace(tzinfo=expires.tzinfo or timezone.utc) > now:
                return JobClaim(False, "held by another worker", attempts)
        if attempts >= 3:
            return JobClaim(False, "exhausted attempts", attempts)

        transaction.set(ref, {
            "status": RUNNING,
            "attempts": attempts + 1,
            "lease_owner": worker,
            "lease_expires_at": now + timedelta(seconds=ttl),
            "started_at": data.get("started_at") or now,
            "updated_at": now,
        }, merge=True)
        return JobClaim(True, "granted", attempts + 1)

    return run(client.transaction())


def renew(job_id: str, *, worker: str, ttl: int = JOB_LEASE_SECONDS) -> bool:
    client = store.get_client()
    ref = client.collection(JOBS).document(job_id)

    @firestore.transactional
    def run(transaction: firestore.Transaction) -> bool:
        snapshot = ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        if data.get("lease_owner") != worker or data.get("status") != RUNNING:
            return False
        transaction.set(ref, {
            "lease_expires_at": store.utcnow() + timedelta(seconds=ttl),
            "updated_at": store.utcnow(),
        }, merge=True)
        return True

    return run(client.transaction())


def log(job_id: str, line: str) -> None:
    """Append one progress line. This is what the browser polls."""
    ref = store.get_client().collection(JOBS).document(job_id)
    ref.set({
        "log": firestore.ArrayUnion([{
            "at": store.utcnow(),
            "line": str(line)[:400],
        }]),
        "updated_at": store.utcnow(),
    }, merge=True)


def trim_log(job_id: str) -> None:
    """Keep the log under the document ceiling, newest kept."""
    ref = store.get_client().collection(JOBS).document(job_id)
    snapshot = ref.get()
    lines = (snapshot.to_dict() or {}).get("log") or []
    if len(lines) > MAX_LOG_LINES:
        ref.set({"log": lines[-MAX_LOG_LINES:]}, merge=True)


def complete(job_id: str, result: Mapping[str, Any] | None = None) -> None:
    store.get_client().collection(JOBS).document(job_id).set(
        store._plain({
            "status": DONE,
            "result": dict(result or {}),
            "finished_at": store.utcnow(),
            "updated_at": store.utcnow(),
        }) | {"lease_owner": None, "lease_expires_at": None},
        merge=True,
    )


def fail(job_id: str, error: str) -> None:
    store.get_client().collection(JOBS).document(job_id).set({
        "status": FAILED,
        "error": str(error)[:800],
        "finished_at": store.utcnow(),
        "updated_at": store.utcnow(),
        "lease_owner": None,
        "lease_expires_at": None,
    }, merge=True)


def get(job_id: str) -> dict[str, Any] | None:
    snapshot = store.get_client().collection(JOBS).document(job_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def recent(limit: int = 20) -> list[dict[str, Any]]:
    query = (
        store.get_client().collection(JOBS)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [snapshot.to_dict() or {} for snapshot in query.stream()]


def active() -> list[dict[str, Any]]:
    query = store.get_client().collection(JOBS).where(
        filter=firestore.FieldFilter("status", "in", [QUEUED, RUNNING])
    )
    return [snapshot.to_dict() or {} for snapshot in query.stream()]
