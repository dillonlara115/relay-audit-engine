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
would erode. `log-touch` is not one: it records that a human already sent
something from their own mailbox, which is the only way the sequence clock can
move while nothing in this codebase can send. It transmits nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app import jobs
from app.config import get_config
from app.console import views
from app.console import auth
from app.console.auth import check_csrf, csrf_token
from app.markets import known_markets
from app.store import firestore as store
from app.tools.pubsub import publish_job

router = APIRouter(prefix="/console")

log = logging.getLogger("relay.console")


def _caller_fingerprint(request: Request) -> str:
    """Guardrail 5: never store or log a raw IP. Salted hash, first 12 chars,
    which is enough to see one source hammering the form and nothing more."""
    raw = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    salt = get_config().report_ip_salt
    if not raw:
        return "unknown"
    return hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()[:12]

_HTML_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-store"}


def _page(html: str) -> Response:
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers=_HTML_HEADERS)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=303)


def _host(request: Request) -> str:
    """The hostname the caller asked for. X-Forwarded-Host first, because
    Firebase Hosting proxies with Host rewritten to the run.app name."""
    forwarded = (request.headers.get("x-forwarded-host") or "").split(",")[0]
    host = forwarded.strip() or request.headers.get("host") or ""
    return host.split(":")[0].strip().lower()


def _back(request: Request, default: str) -> str:
    """Where to send the operator after an action: the page they came from,
    if it is ours. A Referer on another host is discarded, never followed."""
    referer = request.headers.get("referer") or ""
    if referer:
        parsed = urlparse(referer)
        if parsed.netloc.split(":")[0].lower() == _host(request) and parsed.path.startswith("/"):
            query = [(k, v) for k, v in parse_qsl(parsed.query) if k not in ("notice", "detail")]
            return parsed.path + (f"?{urlencode(query)}" if query else "")
    return default


def _with_notice(path: str, code: str, detail: str = "") -> str:
    """A redirect target carrying a notice. Only the session cookie survives
    Firebase Hosting, so a flash rides the query string instead."""
    joiner = "&" if "?" in path else "?"
    return f"{path}{joiner}{urlencode({'notice': code, 'detail': str(detail)[:views.NOTICE_DETAIL_CAP]})}"


def _notice(request: Request) -> tuple[str, str] | None:
    return views.notice_from(request.query_params.get("notice"),
                             request.query_params.get("detail"))


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


@router.post("/login", include_in_schema=False)
async def login(request: Request, password: str = Form(""),
                next: str = Form("/console")) -> Response:
    """Accept the password and start a session.

    The one console route that answers without one, which is what login means.
    A wrong password costs a second before it says so.
    """
    if auth.password_matches(password):
        return auth.grant(request, next)

    await asyncio.sleep(auth.FAILED_ATTEMPT_DELAY_SECONDS)
    log.warning("console login failed from %s", _caller_fingerprint(request))
    return auth.login_response(request, error="That password is not right.",
                               next_path=next)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request, csrf: str = Form(None)) -> Response:
    """Sign out. A POST with CSRF like every other mutating route, so a page
    somebody else controls cannot log the operator out with an image tag."""
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")
    response = _redirect("/console")
    auth.clear_session(response, request)
    return response


@router.get("")
@router.get("/")
async def run_screen(request: Request) -> Response:
    active, batches = await asyncio.gather(
        asyncio.to_thread(jobs.active),
        asyncio.to_thread(store.batch_overview),
    )
    return _page(views.render_run(
        csrf=csrf_token(request), markets=known_markets(),
        active_jobs=active, recent_batches=batches, notice=_notice(request)
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
    return _page(views.render_jobs(await asyncio.to_thread(jobs.recent, 40), notice=_notice(request)))


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
    return _page(views.render_job(record, csrf=csrf_token(request), notice=_notice(request)))


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

    sequences = store.sequences_for_batch(batch_id)

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
            "prospect_id": r.prospect_id,
            "contacts": (prospects.get(r.prospect_id) or {}).get("contacts") or [],
            "sequence": sequences.get(r.prospect_id),
        })
    return rows, segments, check_defs


@router.get("/batches")
async def batches_screen(request: Request, days: int = 14) -> Response:
    """Recent scans. The window is adjustable because a call list does not stop
    being useful on day fifteen, and the default used to hide older ones with
    no way to reach them from the screen."""
    days = max(1, min(days, 3650))
    return _page(views.render_batches(
        await asyncio.to_thread(store.batch_overview, days), days=days, notice=_notice(request)))


@router.get("/batches/{batch_id}")
async def batch_screen(batch_id: str, request: Request) -> Response:
    rows, segments, check_defs = await asyncio.to_thread(_assemble_batch, batch_id)
    overview = await asyncio.to_thread(store.batch_overview)
    progress = next((b for b in overview if b["batch_id"] == batch_id), None)
    return _page(views.render_batch(batch_id, rows, segments, check_defs,
                                    csrf=csrf_token(request), progress=progress, notice=_notice(request)))


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
        findings=findings, evidence=evidence, csrf=csrf_token(request), notice=_notice(request)
    ))


@router.post("/audits/{audit_id}/approve")
async def approve_findings(audit_id: str, request: Request,
                           selected: list[int] = Form(default=[]),
                           csrf: str = Form(None)) -> Response:
    """The human selection rule 7 requires. Approving publishes nothing.

    The model ranks a pool of up to six; this is where a person decides which
    three the contractor reads. The rest become the follow-up material criteria
    section 6 asks for.
    """
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")

    try:
        await asyncio.to_thread(store.approve_report_findings, audit_id, selected)
    except ValueError as exc:
        return _redirect(_with_notice(f"/console/audits/{audit_id}", "not_approved", str(exc)))
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
        return _redirect(_with_notice(f"/console/audits/{audit_id}", "publish_blocked", str(exc)))
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


# ── The outreach ledger ───────────────────────────────────────────────────────


@router.post("/outreach/{prospect_id}/log-touch")
async def log_touch(prospect_id: str, request: Request, audit_id: str = Form(None),
                    csrf: str = Form(None)) -> Response:
    """Record a touch a human already sent by hand.

    This does not send anything and cannot: there is no mail client in this
    process. It moves the sequence clock, which criteria section 6 defines and
    which nothing else can observe while rule 4 stands.

    Suppression is checked first, because logging a touch is an outreach action
    and rule 3 admits no exceptions.
    """
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")

    from app import outreach

    def record() -> str | None:
        prospect = store.get_prospect(prospect_id) or {}
        hit = store.suppression_hit(
            store.load_suppressions(),
            place_id=prospect_id,
            domain=prospect.get("domain"),
            phone=prospect.get("gbp_phone"),
            email=prospect.get("owner_email"),
        )
        if hit:
            return f"This prospect is suppressed ({hit})."

        row = store.get_sequence(prospect_id)
        if row:
            seq = outreach.Sequence.from_dict(row)
        else:
            pool = len(((store.get_draft_findings(audit_id) or {}) if audit_id
                        else {}).get("findings") or [])
            seq = outreach.open_sequence(
                prospect_id, audit_id=audit_id,
                max_touches=outreach.touches_supported(pool) or 1,
            )
        if not seq.is_open:
            return "This outreach sequence is finished."

        sent_at = store.utcnow()
        advanced = outreach.advance(seq, sent_at=sent_at)
        store.add_touch(prospect_id, {
            "ordinal": advanced.touch_count,
            "sent_at": sent_at,
            "audit_id": audit_id or seq.audit_id,
            "channel": "email",
            "logged_via": "console",
        })
        store.save_sequence(advanced)
        return None

    blocked = await asyncio.to_thread(record)
    back = _back(request, "/console/batches")
    if blocked:
        return _redirect(_with_notice(back, "not_recorded", blocked))
    return _redirect(back)


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
