"""What each operator job actually does.

One function per job kind, each taking the job's params and writing progress
lines the browser polls. Every one of them is a thin wrapper over machinery
that already existed and was already tested: the web app is a new way to reach
the engine, not a second implementation of it.

Nothing here sends anything to a contractor. The draft job writes drafts and
stops, because approval is a human act and rule 7 does not have a web
exception.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from app import jobs
from app.store import firestore as store


async def _renew_forever(job_id: str, worker: str) -> None:
    while True:
        await asyncio.sleep(jobs.JOB_LEASE_SECONDS // 3)
        if not await asyncio.to_thread(jobs.renew, job_id, worker=worker):
            return


# ── sweep ─────────────────────────────────────────────────────────────────────


async def run_sweep_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    from app.pipeline import run_sweep

    market = str(params.get("market") or "")
    limit = int(params.get("limit") or 100)
    await asyncio.to_thread(jobs.log, job_id, f"Sweeping {market}, up to {limit} prospects")

    gated = {"pass": 0, "review": 0, "fail": 0}

    def on_ingested(ingest: Any) -> None:
        jobs.log(job_id,
                 f"Places returned {ingest.found} businesses, "
                 f"{ingest.suppressed} suppressed. Gating {len(ingest.records)}.")

    def on_gated(outcome: Any) -> None:
        gated[outcome.result] = gated.get(outcome.result, 0) + 1
        total = sum(gated.values())
        if total % 10 == 0:
            jobs.log(job_id, f"Gated {total}: "
                             f"{gated['pass']} pass, {gated['review']} review, {gated['fail']} fail")

    result = await run_sweep(
        market, limit=limit, on_ingested=on_ingested, on_gated=on_gated
    )
    counts = result.counts
    await asyncio.to_thread(
        jobs.log, job_id,
        f"Done. {counts['found']} ingested, {len(result.continuing)} eligible to audit.",
    )
    return {
        "batch_id": result.batch_id,
        "market_id": result.market_id,
        "eligible": len(result.continuing),
        **counts,
    }


# ── dispatch ──────────────────────────────────────────────────────────────────


async def run_dispatch_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    from app.leases import seed_tasks
    from app.markets import resolve_market
    from app.tools.pubsub import publish_batch

    batch_id = str(params.get("batch_id") or "")
    market = str(params.get("market") or "")
    limit = int(params.get("limit") or 0)

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
        raise RuntimeError("no gated prospects in this market; run a sweep first")

    seeded = await asyncio.to_thread(seed_tasks, batch_id, ids)
    await asyncio.to_thread(jobs.log, job_id, f"Seeded {seeded} tasks in the ledger")
    published = await asyncio.to_thread(publish_batch, batch_id, ids)
    await asyncio.to_thread(
        jobs.log, job_id,
        f"Published {published} audits. Workers pick them up independently.",
    )
    return {"batch_id": batch_id, "published": published, "seeded": seeded}


# ── one audit ─────────────────────────────────────────────────────────────────


async def run_audit_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    from app.agents.audit_graph import audit_via_graph
    from app.markets import resolve_market

    place_id = str(params.get("place_id") or "")
    batch_id = str(params.get("batch_id") or "manual")
    prospect = await asyncio.to_thread(store.get_prospect, place_id)
    if prospect is None:
        raise RuntimeError(f"no prospect {place_id}")
    if prospect.get("suppressed"):
        raise RuntimeError("this prospect is suppressed")

    definitions = await asyncio.to_thread(store.all_check_defs)
    market = resolve_market(str(prospect.get("city") or ""))
    await asyncio.to_thread(jobs.log, job_id,
                            f"Auditing {prospect.get('business_name')}")

    outcome = await audit_via_graph(
        prospect, market, definitions, batch_id=batch_id, persist=True,
        on_event=lambda author, text: jobs.log(job_id, f"{author}: {text}"),
    )
    score = outcome.score
    return {
        "audit_id": outcome.audit_id,
        "total": score.total,
        "band": score.band,
        "segment": score.segment,
        "partial": score.partial,
    }


# ── draft findings ────────────────────────────────────────────────────────────


async def run_draft_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    from app.agents.diagnostician import draft_findings
    from app.ranker import rank

    batch_id = str(params.get("batch_id") or "")
    top = int(params.get("top") or 10)
    only_audit_id = params.get("only_audit_id")

    audits = await asyncio.to_thread(lambda: list(store.audits_for_batch(batch_id)))
    prospects = {}
    for audit in audits:
        pid = audit.get("prospect_id")
        if pid and pid not in prospects:
            prospects[pid] = await asyncio.to_thread(store.get_prospect, pid) or {}
    rows = rank(audits, prospects)
    # "Write talking points" on a single audit page passes only_audit_id, and
    # must draft for that one company alone. Without this the button silently
    # drafted the whole batch's top 40, which is a lot more than it promised.
    rows = [r for r in rows if r.audit_id == only_audit_id] if only_audit_id else rows[:top]

    suppressions = await asyncio.to_thread(store.load_suppressions)
    definitions = {d["code"]: d for d in await asyncio.to_thread(store.all_check_defs)}

    drafted = 0
    skipped = 0
    for row in rows:
        prospect = prospects.get(row.prospect_id) or {}
        # Rule 3: suppression before every outreach action, drafts included.
        hit = store.suppression_hit(
            suppressions, place_id=row.prospect_id, domain=prospect.get("domain"),
            phone=prospect.get("gbp_phone"), email=prospect.get("owner_email"),
        )
        if hit:
            await asyncio.to_thread(jobs.log, job_id,
                                    f"{row.business_name}: suppressed ({hit}), no draft")
            skipped += 1
            continue

        checks = await asyncio.to_thread(store.audit_checks, row.audit_id)
        failures = [
            {**c, "title": definitions.get(c.get("code"), {}).get("title"),
             "points": definitions.get(c.get("code"), {}).get("points", 0)}
            for c in checks if c.get("status") == "fail"
        ]
        failures.sort(key=lambda f: -f["points"])

        diagnosis = await draft_findings(
            business_name=row.business_name, city=row.city or "", failures=failures
        )
        if not diagnosis.ok:
            await asyncio.to_thread(jobs.log, job_id,
                                    f"{row.business_name}: no draft ({diagnosis.error})")
            skipped += 1
            continue

        await asyncio.to_thread(
            store.save_draft_findings, row.audit_id,
            [f.to_dict() for f in diagnosis.findings],
            needs_review=diagnosis.needs_review, model=diagnosis.model,
        )
        flag = " (flagged for review)" if diagnosis.needs_review else ""
        await asyncio.to_thread(jobs.log, job_id,
                                f"{row.business_name}: drafted 3 findings{flag}")
        drafted += 1

    await asyncio.to_thread(
        jobs.log, job_id,
        f"Drafted {drafted}, skipped {skipped}. Every one needs a human to approve it.",
    )
    return {"batch_id": batch_id, "drafted": drafted, "skipped": skipped}


# ── the coordinator ───────────────────────────────────────────────────────────


async def run_agent_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    from app.agents.coordinator import build_coordinator

    prompt = str(params.get("prompt") or "")
    await asyncio.to_thread(jobs.log, job_id, f"Operator: {prompt}")

    runner = InMemoryRunner(build_coordinator(), app_name="relay-sweep")
    session = await runner.session_service.create_session(
        app_name="relay-sweep", user_id="operator"
    )
    final_text: list[str] = []
    calls = 0

    async for event in runner.run_async(
        user_id="operator", session_id=session.id,
        new_message=genai_types.Content(role="user",
                                        parts=[genai_types.Part(text=prompt)]),
    ):
        for part in (event.content.parts if event.content else []) or []:
            if part.function_call:
                calls += 1
                args = dict(part.function_call.args or {})
                await asyncio.to_thread(
                    jobs.log, job_id, f"calling {part.function_call.name}({args})")
            elif part.function_response:
                await asyncio.to_thread(
                    jobs.log, job_id,
                    f"{part.function_response.name} returned "
                    f"{str(part.function_response.response)[:200]}")
            elif part.text and not event.partial:
                final_text.append(part.text.strip())

    return {"tool_calls": calls, "answer": "\n".join(final_text)[:4000]}


RUNNERS = {
    jobs.KIND_SWEEP: run_sweep_job,
    jobs.KIND_DISPATCH: run_dispatch_job,
    jobs.KIND_AUDIT: run_audit_job,
    jobs.KIND_DRAFT: run_draft_job,
    jobs.KIND_AGENT: run_agent_job,
}


async def run_job(job_id: str, *, worker: str) -> tuple[bool, str]:
    """Claim, run, record. Returns (ack, reason) for the Pub/Sub handler."""
    record = await asyncio.to_thread(jobs.get, job_id)
    if record is None:
        return True, "no such job"

    claim = await asyncio.to_thread(jobs.claim, job_id, worker=worker)
    if not claim.granted:
        # Done or exhausted: ack. Held by someone else: come back later.
        return claim.reason != "held by another worker", claim.reason

    runner = RUNNERS.get(record.get("kind") or "")
    if runner is None:
        await asyncio.to_thread(jobs.fail, job_id, f"unknown job kind {record.get('kind')!r}")
        return True, "unknown kind"

    renewer = asyncio.create_task(_renew_forever(job_id, worker))
    try:
        result = await runner(job_id, record.get("params") or {})
    except Exception as exc:  # noqa: BLE001 - a failed job is a record, not a crash
        await asyncio.to_thread(jobs.fail, job_id, f"{type(exc).__name__}: {exc}")
        return False, "job raised"
    finally:
        renewer.cancel()
        try:
            await renewer
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(jobs.trim_log, job_id)

    await asyncio.to_thread(jobs.complete, job_id, result)
    return True, "done"
