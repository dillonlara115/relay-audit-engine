"""Claims and leases. What makes a fanned out batch safe to interrupt.

Three problems arrive the moment audits leave a single process.

1. Pub/Sub delivers at least once, so the same prospect can arrive twice, and
   two workers must not audit it twice.
2. A worker can die mid-audit. Its work must become available again, but only
   after we are reasonably sure it is actually dead.
3. Per host politeness lived inside one Crawler instance. Two workers on the
   same host would each pace themselves at 2 req/sec and the host would see 4.

All three are the same primitive: a lease in Firestore that expires. A holder
that dies stops renewing, the lease lapses, and the next delivery picks it up.

The decision is a pure function over the stored document so it can be tested
exhaustively without a database. The transaction is a thin wrapper around it.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from google.cloud import firestore

from app.store import firestore as store

AUDIT_TASKS = "audit_tasks"
HOST_LEASES = "host_leases"

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Long enough for a slow 25 page crawl plus a render and a Lighthouse run, short
# enough that a killed worker's prospect is retried within one Pub/Sub backoff
# cycle rather than sitting dead for an hour.
AUDIT_LEASE_SECONDS = 600

# A host lease only spans one prospect's crawl.
HOST_LEASE_SECONDS = 420

# Past this a prospect is not retried again, it is dead lettered. Pub/Sub also
# enforces its own limit; this one survives a subscription being recreated.
MAX_ATTEMPTS = 4


def worker_id() -> str:
    """Stable within a process, unique across them."""
    return os.getenv("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def task_id(batch_id: str, prospect_id: str) -> str:
    return f"{batch_id}__{prospect_id}"


@dataclass(frozen=True)
class Decision:
    """What to do with a delivery, decided from the stored state alone."""

    granted: bool
    reason: str
    attempts: int = 0
    holder: str | None = None
    retry: bool = True   # False means ack and drop, do not redeliver

    @property
    def should_ack(self) -> bool:
        """Ack when there is nothing to gain from another delivery."""
        return not self.retry


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def decide_claim(
    state: Mapping[str, Any] | None,
    *,
    now: datetime,
    worker: str,
    max_attempts: int = MAX_ATTEMPTS,
) -> Decision:
    """Pure. Given the stored task document, should this worker take it?

    Reclaiming an expired lease is the whole resumption story: a worker that was
    killed stops renewing, and the next delivery finds a lapsed lease and takes
    over. That is why an expired RUNNING lease is granted rather than skipped.
    """
    data = dict(state or {})
    status = data.get("status") or PENDING
    attempts = int(data.get("attempts") or 0)

    if status == DONE:
        # Duplicate delivery of finished work. Nothing to do, never redeliver.
        return Decision(False, "already done", attempts, retry=False)

    if status == FAILED and attempts >= max_attempts:
        return Decision(False, "exhausted attempts", attempts, retry=False)

    if status == RUNNING:
        expires = _as_utc(data.get("lease_expires_at"))
        holder = data.get("lease_owner")
        if holder == worker:
            # Our own lease, most likely a redelivery while we still hold it.
            return Decision(False, "already held by this worker", attempts, holder, retry=False)
        if expires and expires > now:
            # Someone else is actively working it. Come back later.
            return Decision(False, "held by another worker", attempts, holder, retry=True)
        # Lease lapsed. The holder is presumed dead and this is the takeover.
        if attempts >= max_attempts:
            return Decision(False, "exhausted attempts", attempts, holder, retry=False)
        return Decision(True, "reclaimed an expired lease", attempts + 1, holder)

    if attempts >= max_attempts:
        return Decision(False, "exhausted attempts", attempts, retry=False)

    return Decision(True, "granted", attempts + 1)


def _running_payload(worker: str, now: datetime, attempts: int, ttl: int) -> dict[str, Any]:
    return {
        "status": RUNNING,
        "attempts": attempts,
        "lease_owner": worker,
        "lease_acquired_at": now,
        "lease_expires_at": now + timedelta(seconds=ttl),
        "updated_at": now,
    }


def claim_task(
    batch_id: str,
    prospect_id: str,
    *,
    worker: str,
    ttl: int = AUDIT_LEASE_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> Decision:
    """Atomically take ownership of one prospect's audit, or explain why not."""
    client = store.get_client()
    ref = client.collection(AUDIT_TASKS).document(task_id(batch_id, prospect_id))

    @firestore.transactional
    def run(transaction: firestore.Transaction) -> Decision:
        snapshot = ref.get(transaction=transaction)
        now = store.utcnow()
        decision = decide_claim(
            snapshot.to_dict() if snapshot.exists else None,
            now=now, worker=worker, max_attempts=max_attempts,
        )
        if decision.granted:
            payload = _running_payload(worker, now, decision.attempts, ttl)
            payload.update({"batch_id": batch_id, "prospect_id": prospect_id})
            if not snapshot.exists:
                payload["created_at"] = now
            transaction.set(ref, payload, merge=True)
        return decision

    return run(client.transaction())


def renew_task(batch_id: str, prospect_id: str, *, worker: str,
               ttl: int = AUDIT_LEASE_SECONDS) -> bool:
    """Extend our lease. False means we lost it and should stop working."""
    client = store.get_client()
    ref = client.collection(AUDIT_TASKS).document(task_id(batch_id, prospect_id))

    @firestore.transactional
    def run(transaction: firestore.Transaction) -> bool:
        snapshot = ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        if data.get("lease_owner") != worker or data.get("status") != RUNNING:
            return False
        now = store.utcnow()
        transaction.set(ref, {"lease_expires_at": now + timedelta(seconds=ttl),
                              "updated_at": now}, merge=True)
        return True

    return run(client.transaction())


def complete_task(batch_id: str, prospect_id: str, *, worker: str,
                  audit_id: str | None = None, **extra: Any) -> None:
    """Mark done and drop the lease. Idempotent."""
    now = store.utcnow()
    payload: dict[str, Any] = {
        "status": DONE, "finished_at": now, "updated_at": now,
        "lease_owner": None, "lease_expires_at": None, "last_worker": worker,
    }
    if audit_id:
        payload["audit_id"] = audit_id
    # Extras follow the house rule that absent stays absent. The two lease
    # fields are the exception: they are written as null on purpose, because
    # clearing a lease is the point of this call.
    payload.update({k: v for k, v in extra.items() if v is not None})
    store.get_client().collection(AUDIT_TASKS).document(
        task_id(batch_id, prospect_id)
    ).set(payload, merge=True)


def fail_task(batch_id: str, prospect_id: str, *, worker: str, error: str) -> None:
    """Release the lease and record why. Attempts already counted at claim."""
    now = store.utcnow()
    store.get_client().collection(AUDIT_TASKS).document(
        task_id(batch_id, prospect_id)
    ).set(
        {
            "status": FAILED, "error": error[:500], "updated_at": now,
            "last_worker": worker, "lease_owner": None, "lease_expires_at": None,
        },
        merge=True,
    )


def tasks_for_batch(batch_id: str, status: str | None = None) -> list[dict[str, Any]]:
    query = store.get_client().collection(AUDIT_TASKS).where(
        filter=firestore.FieldFilter("batch_id", "==", batch_id)
    )
    if status:
        query = query.where(filter=firestore.FieldFilter("status", "==", status))
    return [{"id": s.id, **(s.to_dict() or {})} for s in query.stream()]


# ── Host leases ───────────────────────────────────────────────────────────────


def decide_host_lease(
    state: Mapping[str, Any] | None, *, now: datetime, worker: str
) -> Decision:
    """One worker per host at a time, so 2 req/sec stays 2 req/sec."""
    data = dict(state or {})
    holder = data.get("owner")
    expires = _as_utc(data.get("expires_at"))
    if holder and holder != worker and expires and expires > now:
        return Decision(False, "host busy", holder=holder, retry=True)
    return Decision(True, "granted", holder=holder)


def acquire_host(host: str, *, worker: str, ttl: int = HOST_LEASE_SECONDS) -> Decision:
    client = store.get_client()
    ref = client.collection(HOST_LEASES).document(host.replace("/", "_"))

    @firestore.transactional
    def run(transaction: firestore.Transaction) -> Decision:
        snapshot = ref.get(transaction=transaction)
        now = store.utcnow()
        decision = decide_host_lease(
            snapshot.to_dict() if snapshot.exists else None, now=now, worker=worker
        )
        if decision.granted:
            transaction.set(ref, {"host": host, "owner": worker, "acquired_at": now,
                                  "expires_at": now + timedelta(seconds=ttl)})
        return decision

    return run(client.transaction())


def release_host(host: str, *, worker: str) -> None:
    """Only the holder may release, so a late release cannot free someone else's."""
    client = store.get_client()
    ref = client.collection(HOST_LEASES).document(host.replace("/", "_"))

    @firestore.transactional
    def run(transaction: firestore.Transaction) -> None:
        snapshot = ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        if data.get("owner") == worker:
            transaction.set(ref, {"owner": None, "expires_at": None,
                                  "released_at": store.utcnow()}, merge=True)

    run(client.transaction())


def release_task(batch_id: str, prospect_id: str, *, worker: str, reason: str) -> None:
    """Hand a claimed task back without counting it as a failure.

    Used when we won the task but could not get its host lease. The attempt is
    already recorded, so this is not a free retry, but the prospect goes back in
    the pool immediately rather than waiting for our lease to lapse.
    """
    client = store.get_client()
    ref = client.collection(AUDIT_TASKS).document(task_id(batch_id, prospect_id))

    @firestore.transactional
    def run(transaction: firestore.Transaction) -> None:
        snapshot = ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        if data.get("lease_owner") != worker:
            return
        transaction.set(ref, {"status": PENDING, "lease_owner": None,
                              "lease_expires_at": None, "released_reason": reason,
                              "updated_at": store.utcnow()}, merge=True)

    run(client.transaction())


def seed_tasks(batch_id: str, prospect_ids: list[str]) -> int:
    """Write a pending ledger doc per prospect, before anything is published.

    Without this the ledger only learns about a prospect when a worker first
    claims it, so a message lost to congestion is invisible and the batch
    reports complete against a shrunken denominator. Existing docs are left
    alone: seeding again must never knock a running or done task back to
    pending.
    """
    client = store.get_client()
    existing = {t["prospect_id"] for t in tasks_for_batch(batch_id)}
    batch = client.batch()
    now = store.utcnow()
    written = 0
    for prospect_id in prospect_ids:
        if prospect_id in existing:
            continue
        ref = client.collection(AUDIT_TASKS).document(task_id(batch_id, prospect_id))
        batch.set(ref, {"batch_id": batch_id, "prospect_id": prospect_id,
                        "status": PENDING, "attempts": 0, "created_at": now,
                        "updated_at": now})
        written += 1
        if written % 400 == 0:
            batch.commit()
            batch = client.batch()
    batch.commit()
    return written
