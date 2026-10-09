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

    chosen = [str(x) for x in (params.get("prospect_ids") or []) if x]
    if chosen:
        # A person picked these on the Excluded tab and overrode the gate.
        # Suppression is still checked; an override never reaches a
        # suppressed prospect.
        rules = store.load_suppressions()
        ids = []
        for pid in chosen:
            prospect = store.get_prospect(pid) or {}
            if store.suppression_hit(rules, place_id=pid, domain=prospect.get("domain"),
                                     phone=prospect.get("gbp_phone"), email=prospect.get("owner_email")):
                await asyncio.to_thread(jobs.log, job_id, f"Skipped {pid}: suppressed")
                continue
            ids.append(pid)
    else:
        market_id = store.market_id_for(resolve_market(market).name)
        eligible = [
            p for p in store.prospects_for_market(market_id, suppressed=False)
            if p.get("gate_result") in ("pass", "review") or p.get("gate_override") == "pass"
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
    # top 0 means every prospect on the list that has no findings yet.
    top = int(params.get("top") if params.get("top") is not None else 10)
    only_audit_id = params.get("only_audit_id")
    picked = [str(a) for a in params.get("audit_ids") or []]

    audits = await asyncio.to_thread(lambda: list(store.audits_for_batch(batch_id)))
    prospects = {}
    for audit in audits:
        pid = audit.get("prospect_id")
        if pid and pid not in prospects:
            prospects[pid] = await asyncio.to_thread(store.get_prospect, pid) or {}
    rows = rank(audits, prospects)

    # "Draft findings" on a single audit page passes only_audit_id, and must
    # draft for that one company alone. It is the one explicit re-draft.
    if only_audit_id:
        rows = [r for r in rows if r.audit_id == only_audit_id]
    else:
        # Bulk never overwrites. Findings a person already chose, or a report
        # already published from them, are the prospect's record: the follow-up
        # emails read their held-back findings from it. A rerun, or a Pub/Sub
        # redelivery of this job, skips everything already drafted, so it picks
        # up where it left off instead of starting again.
        if picked:
            wanted = set(picked)
            rows = [r for r in rows if r.audit_id in wanted]
        existing = await asyncio.gather(*(asyncio.to_thread(store.get_draft_findings, r.audit_id)
                                          for r in rows))
        fresh = []
        for row, doc in zip(rows, existing):
            if doc:
                await asyncio.to_thread(
                    jobs.log, job_id,
                    f"{row.business_name}: already has findings ({doc.get('status') or 'draft'}), "
                    "skipped. Open it to draft again.")
            else:
                fresh.append(row)
        rows = fresh if picked or top <= 0 else fresh[:top]

    suppressions = await asyncio.to_thread(store.load_suppressions)
    definitions = {d["code"]: d for d in await asyncio.to_thread(store.all_check_defs)}

    counts = {"drafted": 0, "skipped": 0}
    # A few at once: each draft is one model call, and the whole job has to
    # finish inside the push subscription's ten minute ack deadline.
    gate = asyncio.Semaphore(DRAFT_CONCURRENCY)

    async def draft_one(row: Any) -> None:
        async with gate:
            counts[await _draft_row(job_id, row, prospects.get(row.prospect_id) or {},
                                    suppressions, definitions, draft_findings)] += 1

    await asyncio.gather(*(draft_one(r) for r in rows))
    drafted, skipped = counts["drafted"], counts["skipped"]

    await asyncio.to_thread(
        jobs.log, job_id,
        f"Drafted {drafted}, skipped {skipped}. Every one needs a human to approve it.",
    )
    return {"batch_id": batch_id, "drafted": drafted, "skipped": skipped}


DRAFT_CONCURRENCY = 4


async def _draft_row(job_id: str, row: Any, prospect: Mapping[str, Any],
                     suppressions: Any, definitions: Mapping[str, Any], draft_findings: Any) -> str:
    """Draft one prospect's findings. Returns which counter it lands in."""
    # Rule 3: suppression before every outreach action, drafts included.
    hit = store.suppression_hit(
        suppressions, place_id=row.prospect_id, domain=prospect.get("domain"),
        phone=prospect.get("gbp_phone"), email=prospect.get("owner_email"),
    )
    if hit:
        await asyncio.to_thread(jobs.log, job_id, f"{row.business_name}: suppressed ({hit}), no draft")
        return "skipped"

    checks = await asyncio.to_thread(store.audit_checks, row.audit_id)
    failures = [
        {**c, "title": definitions.get(c.get("code"), {}).get("title"),
         "points": definitions.get(c.get("code"), {}).get("points", 0)}
        for c in checks if c.get("status") == "fail"
    ]
    failures.sort(key=lambda f: -f["points"])
    # What passed is ground truth the draft may not contradict.
    passing = [
        {**c, "title": definitions.get(c.get("code"), {}).get("title")}
        for c in checks if c.get("status") == "pass"
    ]

    diagnosis = await draft_findings(
        business_name=row.business_name, city=row.city or "",
        failures=failures, passing=passing,
    )
    if not diagnosis.ok:
        await asyncio.to_thread(jobs.log, job_id, f"{row.business_name}: no draft ({diagnosis.error})")
        return "skipped"

    await asyncio.to_thread(
        store.save_draft_findings, row.audit_id,
        [f.to_dict() for f in diagnosis.findings],
        needs_review=diagnosis.needs_review, model=diagnosis.model,
    )
    flag = " (flagged for review)" if diagnosis.needs_review else ""
    await asyncio.to_thread(jobs.log, job_id, f"{row.business_name}: drafted 3 findings{flag}")
    return "drafted"


# ── retake a screenshot ───────────────────────────────────────────────────────


async def run_screenshot_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """A fresh mobile screenshot of the prospect's homepage, in place of the
    audit's. Full page by default; "viewport" takes just the first screen,
    which is the fix when a full-page capture comes out broken (lazy images,
    a sticky header repeated down the page)."""
    from app.store import evidence as evidence_store
    from app.tools.render import render

    audit_id = str(params.get("audit_id") or "")
    mode = "viewport" if params.get("mode") == "viewport" else "full"
    audit = await asyncio.to_thread(store.get_audit, audit_id)
    if audit is None:
        raise RuntimeError(f"no audit {audit_id}")
    prospect_id = str(audit.get("prospect_id") or "")
    prospect = await asyncio.to_thread(store.get_prospect, prospect_id) or {}
    url = prospect.get("website_url")
    if not url:
        raise RuntimeError("this prospect has no website to screenshot")

    await asyncio.to_thread(jobs.log, job_id, f"Rendering {url} on a phone-sized screen "
                                              f"({'first screen only' if mode == 'viewport' else 'full page'}).")
    result = await render(url, screenshot=mode, image_format="jpeg")
    image = result.screenshot() if result.ok else None
    if not image:
        raise RuntimeError(f"the renderer could not capture the page: {result.error or 'no image'}")
    slug = audit.get("report_slug")
    await asyncio.to_thread(evidence_store.replace_screenshot, prospect_id, audit_id, image,
                            content_type=result.screenshot_mime, source="retaken", report_slug=slug)
    await asyncio.to_thread(jobs.log, job_id, f"Saved a new screenshot ({len(image) // 1024} KB)"
                                              + (", and put it on the published report." if slug else "."))
    return {"audit_id": audit_id}


# ── technical audit: Lighthouse now, and a DataForSEO crawl ───────────────────

# The whole job lives inside one Pub/Sub delivery with a ten minute ack
# deadline, and Lighthouse alone can take two. A crawl still running after this
# is left running: the prospect page offers Check again, which reads the
# finished summary without starting, and paying for, another crawl.
TECHNICAL_POLL_SECONDS = 300
TECHNICAL_POLL_EVERY = 15


async def run_technical_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    import time

    from app.config import get_config
    from app.pipeline import save_lighthouse
    from app.tools import onpage
    from app.tools.pagespeed import analyze

    audit_id = str(params.get("audit_id") or "")
    audit = await asyncio.to_thread(store.get_audit, audit_id)
    if audit is None:
        raise RuntimeError(f"no audit {audit_id}")
    prospect_id = str(audit.get("prospect_id") or "")
    prospect = await asyncio.to_thread(store.get_prospect, prospect_id) or {}
    url, domain = prospect.get("website_url"), prospect.get("domain")
    if not url:
        raise RuntimeError("this prospect has no website to test")

    async def say(line: str) -> None:
        await asyncio.to_thread(jobs.log, job_id, line)

    await say(f"Running Google Lighthouse on {url} as a phone.")
    psi = await analyze(url, fresh=True)
    if psi.ok:
        await save_lighthouse(prospect_id, audit_id, psi)
        await say(f"Lighthouse: performance {psi.performance_score}, accessibility {psi.accessibility_score}, "
                  f"best practices {psi.best_practices_score}, SEO {psi.seo_score}.")
    else:
        await say(f"Lighthouse did not finish: {psi.error}")

    if not domain:
        await say("No domain on record, so no crawl.")
        return {"audit_id": audit_id}
    max_pages = get_config().onpage_max_pages
    try:
        task_id = await asyncio.to_thread(onpage.start, str(domain), max_pages=max_pages)
    except onpage.OnPageUnavailable as exc:
        # Recorded, not raised: a redelivered job would post, and pay for,
        # another crawl.
        await asyncio.to_thread(store.update_audit, audit_id, {"technical": {
            "status": "failed", "error": str(exc), "updated_at": store.utcnow()}})
        await say(f"Crawl not started: {exc}")
        return {"audit_id": audit_id}
    await asyncio.to_thread(store.update_audit, audit_id, {"technical": {
        "status": "crawling", "task_id": task_id, "max_pages": max_pages, "error": None,
        "started_at": store.utcnow()}})
    await say(f"Crawling {domain}, up to {max_pages} pages.")

    deadline = time.monotonic() + TECHNICAL_POLL_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(TECHNICAL_POLL_EVERY)
        try:
            result = await asyncio.to_thread(onpage.summary, task_id)
        except onpage.OnPageUnavailable as exc:
            await say(f"Could not read the crawl yet: {exc}")
            continue
        if result is not None:
            await asyncio.to_thread(store.update_audit, audit_id, {"technical": {
                **onpage.distil(result), "status": "done", "task_id": task_id,
                "finished_at": store.utcnow()}})
            await say("Crawl finished.")
            return {"audit_id": audit_id}
    await say("Still crawling. The prospect page has Check again, which picks up the results "
              "without starting another crawl.")
    return {"audit_id": audit_id}


# ── local reach: a grid of Maps searches around the business ──────────────────


async def run_reach_job(job_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Every search in a run is paid for, so a failure is recorded on the
    audit, not raised: a raised job is redelivered and would search, and pay,
    a second time. A redelivery of a run that already finished does nothing."""
    from app.config import get_config
    from app.tools import reach

    audit_id = str(params.get("audit_id") or "")
    audit = await asyncio.to_thread(store.get_audit, audit_id)
    if audit is None:
        raise RuntimeError(f"no audit {audit_id}")
    if (audit.get("local_reach") or {}).get("job_id") == job_id:
        return {"audit_id": audit_id}
    prospect_id = str(audit.get("prospect_id") or "")
    prospect = await asyncio.to_thread(store.get_prospect, prospect_id) or {}
    lat, lng = prospect.get("lat"), prospect.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        raise RuntimeError("this prospect has no map location on record")

    keyword = params.get("keyword") if params.get("keyword") in reach.KEYWORDS else reach.KEYWORDS[0]
    size = next((s for s in reach.GRID_SIZES if str(s) == str(params.get("size"))), reach.GRID_SIZES[0])
    radius = next((r for r in reach.RADII_MILES if str(r) == str(params.get("radius"))), reach.RADII_MILES[1])
    cap = get_config().reach_max_points
    while size * size > cap and size > 1:
        size -= 2

    async def say(line: str) -> None:
        await asyncio.to_thread(jobs.log, job_id, line)

    await say(f"Searching Google Maps for \"{keyword}\" from {size * size} spots, "
              f"{radius:g} miles out from the business.")
    try:
        result = await reach.run(place_id=prospect_id, domain=str(prospect.get("domain") or ""),
                                 name=str(prospect.get("business_name") or ""), lat=float(lat),
                                 lng=float(lng), keyword=keyword, size=size, radius_miles=radius)
    except reach.ReachUnavailable as exc:
        await asyncio.to_thread(store.update_audit, audit_id, {"local_reach": {
            "status": "failed", "error": str(exc), "job_id": job_id, "updated_at": store.utcnow()}})
        await say(f"Local reach did not run: {exc}")
        return {"audit_id": audit_id}
    # Every key is written each run and lists replace whole under a merge, so
    # a 5 by 5 run leaves nothing of an earlier 7 by 7 behind.
    await asyncio.to_thread(store.update_audit, audit_id, {"local_reach": {
        **result, "status": "done", "error": "", "job_id": job_id, "center": {"lat": lat, "lng": lng},
        "finished_at": store.utcnow()}})
    await say(f"In the top three at {result['top3']} of {result['answered']} spots, "
              f"listed at all at {result['found']}. Cost ${result['cost']:.2f}.")
    return {"audit_id": audit_id}


RUNNERS = {
    jobs.KIND_REACH: run_reach_job,
    jobs.KIND_TECHNICAL: run_technical_job,
    jobs.KIND_SCREENSHOT: run_screenshot_job,
    jobs.KIND_SWEEP: run_sweep_job,
    jobs.KIND_DISPATCH: run_dispatch_job,
    jobs.KIND_AUDIT: run_audit_job,
    jobs.KIND_DRAFT: run_draft_job,
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
