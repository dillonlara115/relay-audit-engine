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

from app.console.auth import authorize as _console_authorize  # noqa: E402
from app.console.routes import router as console_router  # noqa: E402

app.include_router(console_router)


@app.middleware("http")
async def console_gate(request: Request, call_next):
    """Guard every console path before FastAPI parses a body.

    In a handler this check ran after form validation, so an
    unauthenticated POST got a 422 listing the fields it should have sent.
    Here it also covers routes added later without anyone remembering to.
    """
    if request.url.path.startswith("/console"):
        gate = _console_authorize(request)
        if gate is not None:
            return gate
    return await call_next(request)

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


# ── operator dashboard ────────────────────────────────────────────────────────
#
# Read only and internal. The gate: visit once with ?key=<WORKER_SHARED_SECRET>,
# get a cookie holding the secret's hash, get redirected to a clean URL. The
# secret itself never sits in a URL after that first hop, and the cookie cannot
# be replayed against a rotated secret.

import hashlib

from fastapi.responses import RedirectResponse

_DASH_COOKIE = "relay_dash"


def _dash_token() -> str:
    secret = get_config().worker_shared_secret
    return hashlib.sha256(f"dash:{secret}".encode()).hexdigest() if secret else ""


def _dash_authorized(request: Request) -> Response | None:
    """None when authorized, otherwise the response to send instead."""
    token = _dash_token()
    if not token:
        return None  # no secret configured: local dev, IAM is the only gate
    key = request.query_params.get("key")
    if key is not None:
        if key == get_config().worker_shared_secret:
            # Secure follows the real scheme: Cloud Run terminates TLS and
            # forwards http, so the header is the truth, and a local test
            # client speaks plain http where a Secure cookie would vanish.
            https = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
            response = RedirectResponse(url=request.url.path, status_code=303)
            response.set_cookie(_DASH_COOKIE, token, httponly=True, secure=https,
                                max_age=12 * 3600, samesite="lax")
            return response
        return Response(status_code=401)
    if request.cookies.get(_DASH_COOKIE) == token:
        return None
    return Response(status_code=401)


def _batch_overview() -> list:
    """Recent batches, aggregated from the task ledger client side. The ledger
    is the truth about progress; the batches collection only counts sweeps."""
    from datetime import timedelta

    from google.cloud import firestore as gcf

    from app.leases import AUDIT_TASKS

    cutoff = store.utcnow() - timedelta(days=14)
    grouped: dict = {}
    query = store.get_client().collection(AUDIT_TASKS).where(
        filter=gcf.FieldFilter("updated_at", ">=", cutoff)
    )
    for snap in query.stream():
        task = snap.to_dict() or {}
        batch_id = task.get("batch_id")
        if not batch_id:
            continue
        row = grouped.setdefault(batch_id, {"batch_id": batch_id, "total": 0,
                                            "done": 0, "running": 0, "pending": 0,
                                            "failed": 0, "latest": None})
        row["total"] += 1
        row[task.get("status") or "pending"] = row.get(task.get("status") or "pending", 0) + 1
        updated = task.get("updated_at")
        if updated and (row["latest"] is None or updated > row["latest"]):
            row["latest"] = updated
    rows = sorted(grouped.values(), key=lambda r: r["latest"] or store.utcnow(), reverse=True)
    for row in rows:
        row["latest"] = row["latest"].strftime("%b %d %H:%M") if row["latest"] else ""
    return rows


@app.get("/dashboard")
async def dashboard(request: Request) -> Response:
    import asyncio

    from app.report.dashboard import render_overview

    gate = _dash_authorized(request)
    if gate is not None:
        return gate
    batches = await asyncio.to_thread(_batch_overview)
    return Response(content=render_overview(batches),
                    media_type="text/html; charset=utf-8",
                    headers={"X-Robots-Tag": "noindex, nofollow",
                             "Cache-Control": "private, no-store"})


@app.get("/dashboard/{batch_id}")
async def dashboard_batch(batch_id: str, request: Request) -> Response:
    import asyncio

    from app.ranker import rank
    from app.report.dashboard import render_batch

    gate = _dash_authorized(request)
    if gate is not None:
        return gate

    def assemble():
        audits = list(store.audits_for_batch(batch_id))
        prospects = {}
        for audit in audits:
            pid = audit.get("prospect_id")
            if pid and pid not in prospects:
                prospects[pid] = store.get_prospect(pid) or {}
        slugs = {a.get("audit_id"): a.get("report_slug") for a in audits}
        ranked = rank(audits, prospects)
        rows = []
        segments: dict = {}
        for r in ranked:
            segments[r.segment or "incomplete"] = segments.get(r.segment or "incomplete", 0) + 1
            findings = store.get_draft_findings(r.audit_id)
            rows.append({
                "rank": r.rank, "business_name": r.business_name, "city": r.city,
                "segment": r.segment, "scores": dict(r.scores), "phone": r.phone,
                "partial": r.partial, "incumbent_agency": r.incumbent_agency,
                "report_slug": slugs.get(r.audit_id),
                "findings_status": (findings or {}).get("status"),
            })
        return rows, segments

    rows, segments = await asyncio.to_thread(assemble)
    return Response(content=render_batch(batch_id, rows, segments),
                    media_type="text/html; charset=utf-8",
                    headers={"X-Robots-Tag": "noindex, nofollow",
                             "Cache-Control": "private, no-store"})


@app.post("/tick")
async def tick(request: Request) -> Response:
    """The daily tick from Cloud Scheduler. Self-healing, not scheduling.

    Finds batches from the last two days with unfinished tasks whose leases
    have lapsed and republishes them. The same thing the resume command does,
    run on a clock so a batch that stalled overnight is moving again before
    anyone looks at it.
    """
    if not _authorized(request):
        return Response(status_code=401)

    import asyncio
    from datetime import timedelta

    from app.leases import DONE, MAX_ATTEMPTS, RUNNING, AUDIT_TASKS
    from app.tools.pubsub import publish_batch
    from google.cloud import firestore as gcf

    def stalled_by_batch() -> dict[str, list[str]]:
        now = store.utcnow()
        cutoff = now - timedelta(days=2)
        out: dict[str, list[str]] = {}
        query = store.get_client().collection(AUDIT_TASKS).where(
            filter=gcf.FieldFilter("updated_at", ">=", cutoff)
        )
        for snap in query.stream():
            task = snap.to_dict() or {}
            if task.get("status") == DONE:
                continue
            if (task.get("attempts") or 0) >= MAX_ATTEMPTS:
                continue
            if task.get("status") == RUNNING and (task.get("lease_expires_at") or now) > now:
                continue
            batch_id = task.get("batch_id")
            prospect_id = task.get("prospect_id")
            if batch_id and prospect_id:
                out.setdefault(batch_id, []).append(prospect_id)
        return out

    stalled = await asyncio.to_thread(stalled_by_batch)
    republished = 0
    for batch_id, prospect_ids in stalled.items():
        republished += await asyncio.to_thread(publish_batch, batch_id, prospect_ids)
        log.info("tick: republished %d stalled tasks in %s", len(prospect_ids), batch_id)

    return Response(
        status_code=200,
        content=f'{{"republished": {republished}, "batches": {len(stalled)}}}',
        media_type="application/json",
    )


@app.get("/r/{slug}")
def public_report(slug: str, request: Request) -> Response:
    """The one-page report. Public by slug, and only by slug.

    The slug is 16 CSPRNG characters and the only access control this page
    has, which is why it never appears in a sitemap, a log line, or a search
    index. The engine spec's noindex lives in the meta tag and here in the
    header, and the view log stores a salted hash, never the address itself.
    """
    from app.report.publish import log_view, render_by_slug

    if len(slug) < 12 or len(slug) > 24:
        return Response(status_code=404)

    page = render_by_slug(slug)
    if page is None:
        return Response(status_code=404)

    try:
        client_ip = (request.headers.get("x-forwarded-for") or
                     (request.client.host if request.client else "")).split(",")[0].strip()
        log_view(slug, client_ip, request.headers.get("user-agent"))
    except Exception:  # noqa: BLE001 - a failed view log must not break the page
        log.warning("view log failed for %s", slug)

    return Response(
        content=page,
        media_type="text/html; charset=utf-8",
        headers={"X-Robots-Tag": "noindex, nofollow",
                 "Cache-Control": "private, no-store"},
    )


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


@app.post("/pubsub/job")
async def pubsub_job(request: Request) -> Response:
    """Long running operator jobs. Same ack contract as the audit handler."""
    if not _authorized(request):
        return Response(status_code=401)
    try:
        envelope = await request.json()
        message = parse_push(envelope)
    except Exception as exc:  # noqa: BLE001
        log.error("malformed job push: %s", exc)
        return Response(status_code=400)

    job_id = (message.get("job_id")
              or ((envelope.get("message") or {}).get("attributes") or {}).get("job_id"))
    if not job_id:
        return Response(status_code=400)

    from app.job_runner import run_job

    try:
        ack, reason = await run_job(job_id, worker=WORKER)
    except Exception as exc:  # noqa: BLE001 - never ack work that did not finish
        log.exception("job %s failed", job_id)
        return Response(status_code=500, headers={"x-relay-error": type(exc).__name__})

    log.info("job %s -> %s (ack=%s)", job_id, reason, ack)
    return Response(status_code=204 if ack else 409,
                    headers={"x-relay-reason": reason[:80]})


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
