"""The sweep coordinator. The LlmAgent at the top of the architecture.

Its tools are the pipeline stages, and its job is the glue the engine spec
assigns it: route a market sweep end to end and recover from partial failure.
Ingest and gate are one tool because the gate is meaningless on an unsweetened
market and a sweep that skips the gate ships unvetted prospects; the coordinator
chooses when, not what.

The model never touches a prospect site, Firestore, or a score. Every fact it
reports comes back through a tool, which is what keeps guardrail 4 intact: the
coordinator can only relay numbers the pipeline measured.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from google.adk.agents import LlmAgent

from app.config import get_config


def _vertex_location_for_adk() -> None:
    """ADK builds its genai client from the environment, and the environment
    says us-central1 because that is where Firestore and Cloud Run live. The
    model is global-endpoint only, so the model location wins in-process."""
    cfg = get_config()
    if cfg.use_vertexai and cfg.model_location:
        os.environ["GOOGLE_CLOUD_LOCATION"] = cfg.model_location


# ── Tools. Plain async functions; ADK wraps them. ────────────────────────────


async def sweep_market(market: str, limit: int = 100) -> dict[str, Any]:
    """Ingest a metro from Places and run the fit gate on every prospect.

    Returns counts and the batch id. Only prospects whose gate result is pass
    or review are eligible for auditing.
    """
    from app.pipeline import run_sweep

    result = await run_sweep(market, limit=limit)
    return {
        "batch_id": result.batch_id,
        "market_id": result.market_id,
        **result.counts,
        "eligible_for_audit": len(result.continuing),
    }


async def dispatch_audits(batch_id: str, market: str, limit: int = 0) -> dict[str, Any]:
    """Fan the batch out: one Pub/Sub message per gated prospect. Workers pick
    them up independently. limit 0 means every eligible prospect."""
    from app.leases import seed_tasks
    from app.markets import resolve_market
    from app.store import firestore as store
    from app.tools.pubsub import publish_batch

    market_id = store.market_id_for(resolve_market(market).name)
    eligible = [
        p for p in store.prospects_for_market(market_id, suppressed=False)
        if p.get("gate_result") in ("pass", "review")
    ]
    eligible.sort(key=lambda p: -(p.get("review_count") or 0))
    if limit:
        eligible = eligible[:limit]
    ids = [p["place_id"] for p in eligible]
    if not ids:
        return {"published": 0, "error": "no gated prospects; run sweep_market first"}
    seeded = await asyncio.to_thread(seed_tasks, batch_id, ids)
    published = await asyncio.to_thread(publish_batch, batch_id, ids)
    return {"published": published, "seeded": seeded, "batch_id": batch_id}


async def batch_status(batch_id: str) -> dict[str, Any]:
    """Where the fan-out has got to: done, running, pending, failed counts,
    plus stale leases whose worker looks dead."""
    from app.leases import RUNNING, tasks_for_batch
    from app.store import firestore as store

    tasks = await asyncio.to_thread(tasks_for_batch, batch_id)
    if not tasks:
        return {"batch_id": batch_id, "error": "no tasks recorded for this batch"}
    counts: dict[str, int] = {}
    now = store.utcnow()
    stale = 0
    for task in tasks:
        status = task.get("status") or "pending"
        counts[status] = counts.get(status, 0) + 1
        if status == RUNNING:
            expires = task.get("lease_expires_at")
            if expires is None or expires <= now:
                stale += 1
    return {
        "batch_id": batch_id,
        "total": len(tasks),
        "counts": counts,
        "stale_leases": stale,
        "finished": counts.get("done", 0) == len(tasks),
    }


async def resume_batch(batch_id: str) -> dict[str, Any]:
    """Republish unfinished prospects. Safe against duplicates: claims decide,
    not messages. Use when a batch stalls or a worker died."""
    from app.leases import DONE, MAX_ATTEMPTS, RUNNING, tasks_for_batch
    from app.store import firestore as store
    from app.tools.pubsub import publish_batch

    tasks = await asyncio.to_thread(tasks_for_batch, batch_id)
    now = store.utcnow()
    unfinished = []
    for task in tasks:
        if task.get("status") == DONE:
            continue
        if (task.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        if task.get("status") == RUNNING and (task.get("lease_expires_at") or now) > now:
            continue
        unfinished.append(task["prospect_id"])
    if not unfinished:
        return {"republished": 0, "note": "nothing to resume"}
    published = await asyncio.to_thread(publish_batch, batch_id, unfinished)
    return {"republished": published}


async def wait(seconds: int) -> dict[str, Any]:
    """Pause between status checks. Audits take a minute or two each and the
    batch runs at most four at a time, so poll patiently."""
    await asyncio.sleep(max(1, min(int(seconds), 120)))
    return {"waited_seconds": seconds}


async def rank_call_list(batch_id: str, top: int = 10) -> dict[str, Any]:
    """The ranked call list: segment priority first, emptiest Booked bucket
    first within a segment. Returns the top rows and the segment counts."""
    from app.ranker import rank
    from app.store import firestore as store

    audits = await asyncio.to_thread(lambda: list(store.audits_for_batch(batch_id)))
    if not audits:
        return {"batch_id": batch_id, "error": "no audits recorded"}
    prospects = {}
    for audit in audits:
        pid = audit.get("prospect_id")
        if pid and pid not in prospects:
            prospects[pid] = await asyncio.to_thread(store.get_prospect, pid) or {}
    rows = rank(audits, prospects)
    segments: dict[str, int] = {}
    for row in rows:
        segments[row.segment or "incomplete"] = segments.get(row.segment or "incomplete", 0) + 1
    return {
        "batch_id": batch_id,
        "segments": segments,
        "call_list": [
            {
                "rank": r.rank, "business_name": r.business_name, "city": r.city,
                "segment": r.segment or "incomplete", "phone": r.phone,
                "scores": dict(r.scores), "incumbent_agency": r.incumbent_agency,
            }
            for r in rows[:top]
        ],
    }


INSTRUCTION = """You are the sweep coordinator for a prospecting engine that audits
residential roofing contractors. You orchestrate; the tools do the work.

To run a full sweep of a metro:
1. sweep_market to ingest and gate.
2. dispatch_audits with a new batch id (use the one sweep_market returns).
3. Poll batch_status, using wait between polls (30 to 60 seconds). Audits are
   crawls of real websites at 2 requests per second, so patience is correct.
4. If the batch stalls (counts stop moving across two polls, or stale_leases
   is nonzero and pending is zero), call resume_batch once, then keep polling.
5. When finished, rank_call_list and report: segment counts, then the top
   prospects with rank, name, phone, and segment.

Report only numbers the tools returned. If a tool reports an error, say what
failed and stop rather than improvising. Never use an em-dash in your output."""


def build_coordinator() -> LlmAgent:
    _vertex_location_for_adk()
    return LlmAgent(
        name="sweep_coordinator",
        model=get_config().gemini_model,
        description="Runs a metro sweep end to end and reports the call list.",
        instruction=INSTRUCTION,
        tools=[sweep_market, dispatch_audits, batch_status, resume_batch, wait,
               rank_call_list],
    )
