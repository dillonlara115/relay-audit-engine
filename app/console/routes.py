"""Console routes. Every mutating action is a POST that starts a job.

Two shapes only:
  GET  renders a page from Firestore
  POST validates CSRF, creates a job, publishes it, redirects to the job view

The session gate is middleware, not a line in each handler. FastAPI validates a
form body before the handler runs, so a per-handler check answered an
unauthenticated POST with a 422 describing the fields it wanted. Middleware also
means a route added later is covered by construction rather than by memory.

Nothing long runs inside a request. Cloud Run throttles CPU once a response is
sent, so a sweep started in a handler would be killed halfway; instead the job
goes over Pub/Sub to a worker, which is the same path audits already take.

There is no send route. Rule 4 is drafts only, and a button is how that rule
would erode.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app import jobs
from app.console import views
from app.console.auth import check_csrf, csrf_token
from app.markets import known_markets
from app.store import firestore as store
from app.tools.pubsub import publish_job

router = APIRouter(prefix="/console")

_HTML_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-store"}


def _page(html: str) -> Response:
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers=_HTML_HEADERS)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=303)


async def _start(request: Request, csrf: str | None, kind: str, params: dict[str, Any],
                 label: str) -> Response:
    """Validate, record the job, publish it, send the operator to watch it."""
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")
    job_id = await asyncio.to_thread(jobs.create, kind, params, label=label)
    try:
        await asyncio.to_thread(publish_job, job_id, kind)
    except Exception as exc:  # noqa: BLE001 - a job nobody will run must say so
        await asyncio.to_thread(jobs.fail, job_id, f"could not queue: {exc}")
    return _redirect(f"/console/jobs/{job_id}")


# ── Run ───────────────────────────────────────────────────────────────────────


@router.get("")
@router.get("/")
async def run_screen(request: Request) -> Response:
    active, batches = await asyncio.gather(
        asyncio.to_thread(jobs.active),
        asyncio.to_thread(store.batch_overview),
    )
    return _page(views.render_run(
        csrf=csrf_token(request), markets=known_markets(),
        active_jobs=active, recent_batches=batches,
    ))


@router.post("/sweep")
async def start_sweep(request: Request, market: str = Form(...),
                      limit: int = Form(100), csrf: str = Form(None)) -> Response:
    return await _start(request, csrf, jobs.KIND_SWEEP,
                        {"market": market, "limit": max(1, min(int(limit), 300))},
                        f"Sweep {market}")


@router.post("/agent")
async def start_agent(request: Request, prompt: str = Form(...),
                      csrf: str = Form(None)) -> Response:
    return await _start(request, csrf, jobs.KIND_AGENT, {"prompt": prompt[:2000]},
                        "Coordinator run")


@router.post("/dispatch")
async def start_dispatch(request: Request, batch_id: str = Form(...),
                         market: str = Form(...), limit: int = Form(0),
                         csrf: str = Form(None)) -> Response:
    return await _start(request, csrf, jobs.KIND_DISPATCH,
                        {"batch_id": batch_id, "market": market,
                         "limit": max(0, min(int(limit), 300))},
                        f"Dispatch {batch_id}")


@router.post("/draft")
async def start_draft(request: Request, batch_id: str = Form(...),
                      top: int = Form(10), csrf: str = Form(None)) -> Response:
    return await _start(request, csrf, jobs.KIND_DRAFT,
                        {"batch_id": batch_id, "top": max(1, min(int(top), 40))},
                        f"Draft findings for {batch_id}")


# ── Jobs ──────────────────────────────────────────────────────────────────────


@router.get("/jobs")
async def jobs_screen(request: Request) -> Response:
    return _page(views.render_jobs(await asyncio.to_thread(jobs.recent, 40)))


@router.get("/jobs/{job_id}.json")
async def job_json(job_id: str, request: Request) -> Response:
    record = await asyncio.to_thread(jobs.get, job_id)
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "job_id": job_id,
        "status": record.get("status"),
        "log": [{"line": entry.get("line", "")} for entry in (record.get("log") or [])],
        "result": record.get("result") or {},
        "error": record.get("error"),
    }, headers={"Cache-Control": "no-store"})


@router.get("/jobs/{job_id}")
async def job_screen(job_id: str, request: Request) -> Response:
    record = await asyncio.to_thread(jobs.get, job_id)
    if record is None:
        return Response(status_code=404)
    return _page(views.render_job(record, csrf=csrf_token(request)))


# ── Batches ───────────────────────────────────────────────────────────────────


def _assemble_batch(
    batch_id: str,
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    from app.ranker import rank

    audits = list(store.audits_for_batch(batch_id))
    prospects: dict[str, Any] = {}
    for audit in audits:
        pid = audit.get("prospect_id")
        if pid and pid not in prospects:
            prospects[pid] = store.get_prospect(pid) or {}
    slugs = {a.get("audit_id"): a.get("report_slug") for a in audits}

    # Every check's status per audit, so the batch page can filter by them
    # client side ("show only businesses failing C16, footer copyright") without
    # a round trip per filter change. One extra Firestore read per audit, same
    # cost class as the findings lookup already done here.
    checks_by_audit = {
        a.get("audit_id"): {
            c.get("code"): c.get("status")
            for c in store.audit_checks(a.get("audit_id"))
            if c.get("code")
        }
        for a in audits
    }

    check_defs = sorted(
        (d for d in store.all_check_defs() if d.get("enabled")),
        key=lambda d: d.get("sort_order", 0),
    )

    rows, segments = [], {}
    for r in rank(audits, prospects):
        segments[r.segment or "incomplete"] = segments.get(r.segment or "incomplete", 0) + 1
        findings = store.get_draft_findings(r.audit_id)
        rows.append({
            "rank": r.rank, "audit_id": r.audit_id, "business_name": r.business_name,
            "city": r.city, "segment": r.segment, "scores": dict(r.scores),
            "phone": r.phone, "partial": r.partial,
            "incumbent_agency": r.incumbent_agency,
            "report_slug": slugs.get(r.audit_id),
            "findings_status": (findings or {}).get("status"),
            "checks": checks_by_audit.get(r.audit_id) or {},
        })
    return rows, segments, check_defs


@router.get("/batches")
async def batches_screen(request: Request) -> Response:
    return _page(views.render_batches(await asyncio.to_thread(store.batch_overview)))


@router.get("/batches/{batch_id}")
async def batch_screen(batch_id: str, request: Request) -> Response:
    rows, segments, check_defs = await asyncio.to_thread(_assemble_batch, batch_id)
    overview = await asyncio.to_thread(store.batch_overview)
    progress = next((b for b in overview if b["batch_id"] == batch_id), None)
    return _page(views.render_batch(batch_id, rows, segments, check_defs,
                                    csrf=csrf_token(request), progress=progress))


# ── One audit, and the human decisions ────────────────────────────────────────


def _evidence_with_urls(evidence_store: Any, audit_id: str) -> list[dict[str, Any]]:
    """Evidence rows with a viewable URL attached.

    The bucket has public access prevention on, so a stored screenshot is only
    reachable through a signed URL minted per page load. Same approach the
    published report uses. A failure to sign is not fatal: the row still lists
    what was captured, it just cannot be shown inline.
    """
    rows = []
    for row in evidence_store.audit_evidence(audit_id):
        row = dict(row)
        path = row.get("gcs_path")
        if path:
            try:
                row["url"] = evidence_store.signed_url(path)
            except Exception as exc:  # noqa: BLE001 - show the row, not a crash
                row["url_error"] = f"{type(exc).__name__}: {exc}"[:120]
        rows.append(row)
    return rows


@router.get("/audits/{audit_id}")
async def audit_screen(audit_id: str, request: Request) -> Response:

    def load() -> Any:
        from app.store import evidence as evidence_store

        audit = store.get_audit(audit_id)
        if audit is None:
            return None
        audit = {"audit_id": audit_id, **audit}
        return (
            audit,
            store.get_prospect(str(audit.get("prospect_id"))) or {},
            store.audit_checks(audit_id),
            {d["code"]: d for d in store.all_check_defs()},
            store.get_draft_findings(audit_id),
            _evidence_with_urls(evidence_store, audit_id),
        )

    loaded = await asyncio.to_thread(load)
    if loaded is None:
        return Response(status_code=404)
    audit, prospect, checks, definitions, findings, evidence = loaded
    return _page(views.render_audit(
        audit=audit, prospect=prospect, checks=checks, definitions=definitions,
        findings=findings, evidence=evidence, csrf=csrf_token(request),
    ))


@router.post("/audits/{audit_id}/approve")
async def approve_findings(audit_id: str, request: Request,
                           csrf: str = Form(None)) -> Response:
    """The human selection rule 7 requires. Approving publishes nothing."""
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")

    def approve() -> None:
        store.get_client().collection(store.REPORT_FINDINGS).document(audit_id).set(
            {"status": "approved", "approved_at": store.utcnow(),
             "approved_via": "console"}, merge=True
        )

    await asyncio.to_thread(approve)
    return _redirect(f"/console/audits/{audit_id}")


@router.post("/audits/{audit_id}/publish")
async def publish_report(audit_id: str, request: Request,
                         csrf: str = Form(None)) -> Response:
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")

    from app.report.publish import PublishBlocked, publish

    try:
        await asyncio.to_thread(publish, audit_id)
    except PublishBlocked as exc:
        return _page(views.shell(
            "Publish blocked",
            f'<h1>Publish blocked</h1><div class="banner">{views.esc(exc)}</div>'
            f'<p><a href="/console/audits/{views.esc(audit_id)}">Back to the audit</a></p>',
            active="batches",
        ))
    return _redirect(f"/console/audits/{audit_id}")


@router.post("/audits/{audit_id}/draft")
async def draft_one(audit_id: str, request: Request, csrf: str = Form(None)) -> Response:
    audit = await asyncio.to_thread(store.get_audit, audit_id)
    if audit is None:
        return Response(status_code=404)
    return await _start(request, csrf, jobs.KIND_DRAFT,
                        {"batch_id": audit.get("batch_id"), "top": 40,
                         "only_audit_id": audit_id},
                        "Draft findings")


@router.post("/audits/{audit_id}/reaudit")
async def reaudit(audit_id: str, request: Request, csrf: str = Form(None)) -> Response:
    audit = await asyncio.to_thread(store.get_audit, audit_id)
    if audit is None:
        return Response(status_code=404)
    return await _start(request, csrf, jobs.KIND_AUDIT,
                        {"place_id": audit.get("prospect_id"),
                         "batch_id": audit.get("batch_id") or "manual"},
                        "Re-audit")


# ── Suppression ───────────────────────────────────────────────────────────────


@router.post("/suppress")
async def suppress(request: Request, value: str = Form(...),
                   match_type: str = Form("place_id"), reason: str = Form("requested"),
                   csrf: str = Form(None)) -> Response:
    """Permanent and immediate, per the outreach rules. No undo here on purpose."""
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")

    def apply() -> None:
        store.add_suppression(match_type, value, reason)
        if match_type == "place_id":
            store.mark_suppressed(value, reason)

    await asyncio.to_thread(apply)
    return _redirect("/console")
