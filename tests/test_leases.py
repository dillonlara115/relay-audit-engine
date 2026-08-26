"""Claim and lease decisions. Pure functions, no Firestore.

This is the logic that decides whether a fanned out batch double-works a
prospect, drops one, or hangs forever holding a dead worker's lease. Every
branch is enumerated here because the failure modes are invisible until a
batch is halfway through and a worker has already died.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.leases import (
    DONE,
    FAILED,
    MAX_ATTEMPTS,
    PENDING,
    RUNNING,
    decide_claim,
    decide_host_lease,
    task_id,
    worker_id,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
ME = "worker-a"
THEM = "worker-b"


def state(**overrides):
    base = {"status": PENDING, "attempts": 0}
    base.update(overrides)
    return base


def claim(st=None, *, worker=ME, now=NOW, max_attempts=MAX_ATTEMPTS):
    return decide_claim(st, now=now, worker=worker, max_attempts=max_attempts)


# ── First delivery ────────────────────────────────────────────────────────────


def test_an_unseen_prospect_is_granted():
    d = claim(None)
    assert d.granted and d.attempts == 1
    assert d.reason == "granted"


def test_a_pending_task_is_granted():
    d = claim(state())
    assert d.granted and d.attempts == 1


def test_attempts_increment_on_each_grant():
    assert claim(state(attempts=2)).attempts == 3


# ── Duplicate delivery ────────────────────────────────────────────────────────


def test_finished_work_is_acked_not_redelivered():
    """Pub/Sub delivers at least once. A duplicate of done work must not run
    again, and must not come back a third time either."""
    d = claim(state(status=DONE, attempts=1))
    assert not d.granted
    assert d.should_ack, "acking is what stops the redelivery loop"
    assert d.reason == "already done"


def test_a_redelivery_while_we_still_hold_it_is_acked():
    d = claim(state(status=RUNNING, lease_owner=ME,
                    lease_expires_at=NOW + timedelta(minutes=5), attempts=1))
    assert not d.granted and d.should_ack
    assert "this worker" in d.reason


# ── Concurrency ───────────────────────────────────────────────────────────────


def test_a_live_lease_held_by_another_worker_blocks_and_retries():
    d = claim(state(status=RUNNING, lease_owner=THEM,
                    lease_expires_at=NOW + timedelta(minutes=5), attempts=1))
    assert not d.granted
    assert d.retry, "someone else is working it, come back later"
    assert not d.should_ack
    assert d.holder == THEM


# ── The killed worker ─────────────────────────────────────────────────────────


def test_an_expired_lease_is_reclaimed():
    """The whole resumption story. A killed worker stops renewing, its lease
    lapses, and the next delivery takes over."""
    d = claim(state(status=RUNNING, lease_owner=THEM,
                    lease_expires_at=NOW - timedelta(seconds=1), attempts=1))
    assert d.granted
    assert d.reason == "reclaimed an expired lease"
    assert d.attempts == 2
    assert d.holder == THEM


def test_a_running_task_with_no_lease_recorded_is_reclaimed():
    """A worker that died between writing status and writing its lease."""
    d = claim(state(status=RUNNING, lease_owner=THEM, attempts=1))
    assert d.granted


def test_a_lease_expiring_exactly_now_is_reclaimable():
    d = claim(state(status=RUNNING, lease_owner=THEM, lease_expires_at=NOW, attempts=1))
    assert d.granted


def test_a_naive_timestamp_is_treated_as_utc():
    """Firestore can hand back a naive datetime. Comparing it to an aware one
    raises, and a raise inside a transaction is a stuck prospect."""
    d = claim(state(status=RUNNING, lease_owner=THEM, attempts=1,
                    lease_expires_at=datetime(2026, 8, 27, 12, 5)))
    assert not d.granted and d.retry, "still live, five minutes ahead of NOW"


# ── Exhaustion ────────────────────────────────────────────────────────────────


def test_a_task_at_the_attempt_ceiling_is_dropped():
    d = claim(state(attempts=MAX_ATTEMPTS))
    assert not d.granted
    assert d.should_ack, "stop redelivering, let the dead letter topic have it"
    assert d.reason == "exhausted attempts"


def test_an_expired_lease_at_the_ceiling_is_not_reclaimed():
    """A prospect that kills every worker that touches it must stop being
    handed to new ones."""
    d = claim(state(status=RUNNING, lease_owner=THEM, attempts=MAX_ATTEMPTS,
                    lease_expires_at=NOW - timedelta(seconds=1)))
    assert not d.granted and d.should_ack


def test_a_failed_task_below_the_ceiling_is_retried():
    d = claim(state(status=FAILED, attempts=1))
    assert d.granted and d.attempts == 2


def test_a_failed_task_at_the_ceiling_is_dropped():
    d = claim(state(status=FAILED, attempts=MAX_ATTEMPTS))
    assert not d.granted and d.should_ack


@pytest.mark.parametrize("attempts", [0, 1, 2, 3])
def test_every_attempt_below_the_ceiling_is_granted(attempts):
    assert claim(state(attempts=attempts), max_attempts=4).granted


# ── Host leases ───────────────────────────────────────────────────────────────


def host(st=None, *, worker=ME, now=NOW):
    return decide_host_lease(st, now=now, worker=worker)


def test_an_unheld_host_is_granted():
    assert host(None).granted
    assert host({"owner": None}).granted


def test_a_host_held_by_another_worker_is_refused():
    """Two workers pacing themselves at 2 req/sec would show the host 4."""
    d = host({"owner": THEM, "expires_at": NOW + timedelta(minutes=5)})
    assert not d.granted and d.retry
    assert d.holder == THEM


def test_a_host_lease_we_already_hold_is_regranted():
    assert host({"owner": ME, "expires_at": NOW + timedelta(minutes=5)}).granted


def test_an_expired_host_lease_is_available():
    assert host({"owner": THEM, "expires_at": NOW - timedelta(seconds=1)}).granted


def test_a_host_lease_with_no_expiry_is_available():
    """A worker that died before writing its expiry must not lock a host out."""
    assert host({"owner": THEM}).granted


# ── Identity ──────────────────────────────────────────────────────────────────


def test_task_ids_are_unique_per_batch_and_prospect():
    assert task_id("b1", "p1") == "b1__p1"
    assert task_id("b1", "p1") != task_id("b2", "p1")


def test_worker_ids_differ_between_calls(monkeypatch):
    monkeypatch.delenv("WORKER_ID", raising=False)
    assert worker_id() != worker_id()


def test_worker_id_can_be_pinned_by_env(monkeypatch):
    monkeypatch.setenv("WORKER_ID", "fixed-1")
    assert worker_id() == "fixed-1"
