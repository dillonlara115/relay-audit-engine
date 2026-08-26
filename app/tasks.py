"""One prospect's audit, as a unit of work a worker can pick up and drop.

This is the layer between Pub/Sub and the audit pipeline. It owns the claim, the
host lease, lease renewal, and the decision about whether a delivery should be
acked or left for redelivery. The audit itself knows none of that.

Ack semantics matter more than they look. Acking something that did not finish
loses a prospect silently. Nacking something unfixable loops it until the dead
letter queue catches it, which is noisy but safe. When in doubt this module
nacks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

from app.leases import (
    AUDIT_LEASE_SECONDS,
    acquire_host,
    claim_task,
    complete_task,
    fail_task,
    release_host,
    release_task,
    renew_task,
)
from app.agents.audit_graph import audit_via_graph
from app.markets import resolve_market
from app.store import firestore as store
from app.tools.crawl import registrable_host

# Renew at a third of the TTL so two renewals can fail before the lease lapses.
RENEW_EVERY_SECONDS = AUDIT_LEASE_SECONDS // 3


@dataclass
class TaskOutcome:
    ack: bool
    reason: str
    prospect_id: str
    batch_id: str
    audit_id: str | None = None
    total: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


async def _renew_forever(batch_id: str, prospect_id: str, worker: str) -> None:
    """Keep the lease alive while we work.

    If this loop finds the lease is no longer ours, it stops renewing rather
    than stealing it back. Someone else took over because we looked dead, and
    two workers on one prospect is worse than a duplicated audit.
    """
    while True:
        await asyncio.sleep(RENEW_EVERY_SECONDS)
        held = await asyncio.to_thread(
            renew_task, batch_id, prospect_id, worker=worker
        )
        if not held:
            return


async def run_audit_task(
    batch_id: str,
    prospect_id: str,
    *,
    worker: str,
    definitions: list[Mapping[str, Any]] | None = None,
) -> TaskOutcome:
    """Claim, audit, release. Never raises."""
    prospect = await asyncio.to_thread(store.get_prospect, prospect_id)
    if prospect is None:
        # Nothing will make this message valid on a later delivery.
        return TaskOutcome(True, "no such prospect", prospect_id, batch_id)

    if prospect.get("suppressed"):
        return TaskOutcome(True, "suppressed", prospect_id, batch_id)

    decision = await asyncio.to_thread(claim_task, batch_id, prospect_id, worker=worker)
    if not decision.granted:
        return TaskOutcome(decision.should_ack, decision.reason, prospect_id, batch_id)

    website = prospect.get("website_url")
    host = registrable_host(website) if website else None

    if host:
        lease = await asyncio.to_thread(acquire_host, host, worker=worker)
        if not lease.granted:
            # Politeness wins over throughput. Give the prospect back and let a
            # later delivery try once the other worker is done with this host.
            await asyncio.to_thread(
                release_task, batch_id, prospect_id, worker=worker, reason="host busy"
            )
            return TaskOutcome(False, f"host {host} busy", prospect_id, batch_id)

    definitions = definitions or await asyncio.to_thread(store.all_check_defs)
    market = resolve_market(str(prospect.get("city") or ""))
    renewer = asyncio.create_task(_renew_forever(batch_id, prospect_id, worker))

    try:
        # The audit runs through the ADK graph: recon (SequentialAgent stage),
        # the inspector fan (ParallelAgent), then checks and scoring. Same
        # functions, same persistence, one architecture.
        outcome = await audit_via_graph(
            prospect, market, definitions, batch_id=batch_id, persist=True
        )
    except Exception as exc:  # noqa: BLE001 - one prospect must not end the batch
        await asyncio.to_thread(
            fail_task, batch_id, prospect_id, worker=worker,
            error=f"{type(exc).__name__}: {exc}",
        )
        return TaskOutcome(False, "audit raised", prospect_id, batch_id,
                           error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        renewer.cancel()
        try:
            await renewer
        except asyncio.CancelledError:
            pass
        if host:
            await asyncio.to_thread(release_host, host, worker=worker)

    await asyncio.to_thread(
        complete_task, batch_id, prospect_id, worker=worker,
        audit_id=outcome.audit_id, total=outcome.score.total,
        band=outcome.score.band, segment=outcome.score.segment,
    )
    await asyncio.to_thread(store.bump_batch_counts, batch_id, audited=1)

    return TaskOutcome(True, "audited", prospect_id, batch_id,
                       audit_id=outcome.audit_id, total=outcome.score.total)
