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

There is one send route, `outreach/{prospect_id}/send`, and it sends one email
a person has read and pressed Send on, from their own mailbox (rule 4 as
amended Sep 17, 2026). It is the only caller of `gmail.send_message` in the
package and a test holds it to that. `log-touch` records a send made from
somewhere else so the sequence clock moves; it transmits nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any, Mapping
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


def _soft(fn: Any, default: Any, *args: Any, **kwargs: Any) -> Any:
    """A read whose failure degrades one card rather than the whole page.
    Logged, never raised: an unpatched store call in a test, or a Firestore
    hiccup in the ledger, must not take the prospect page down with it."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - by design
        log.warning("%s failed: %s: %s", getattr(fn, "__name__", fn), type(exc).__name__, exc)
        return default


def _report_url(request: Request, slug: str | None) -> str | None:
    """The address a contractor opens. The configured public host when there
    is one, else the host this request arrived on, which behind Firebase is
    the custom domain and not the run.app name."""
    if not slug:
        return None
    raw = get_config().public_report_host
    first = next((part for part in re.split(r"[,;\s]+", raw) if part.strip()), "")
    host = first.split(":")[0] if first else (_host(request) or request.url.netloc)
    scheme = "https" if first else (request.headers.get("x-forwarded-proto") or request.url.scheme)
    return f"{scheme}://{host}/{slug}"


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


# Concurrent Firestore reads per call-list load. Sixteen keeps a hundred-audit
# sweep to seven rounds of round trips instead of three hundred.
READ_WORKERS = 16


def _assemble_batch(
    batch_id: str,
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    from app.ranker import rank

    from concurrent.futures import ThreadPoolExecutor

    audits = list(store.audits_for_batch(batch_id))
    audit_ids = [a.get("audit_id") for a in audits]
    prospect_ids = list({a.get("prospect_id") for a in audits if a.get("prospect_id")})
    slugs = {a.get("audit_id"): a.get("report_slug") for a in audits}

    # Three reads per audit: the prospect, every check's status (so the page
    # can filter by check without a round trip), and the findings doc. Done
    # one after another they cost a Firestore round trip each, ninety in a
    # row for a thirty-audit sweep and five seconds on screen. Done at once
    # they cost about one. The reads go through store.* by name so a test
    # can stub them, and the pool is bounded so a hundred-audit sweep does
    # not open a hundred connections.
    with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
        prospect_futures = {pid: pool.submit(store.get_prospect, pid) for pid in prospect_ids}
        checks_futures = {aid: pool.submit(store.audit_checks, aid) for aid in audit_ids}
        findings_futures = {aid: pool.submit(store.get_draft_findings, aid) for aid in audit_ids}
        sequences_future = pool.submit(store.sequences_for_batch, batch_id)
        defs_future = pool.submit(store.all_check_defs)
        prospects: dict[str, Any] = {pid: f.result() or {} for pid, f in prospect_futures.items()}
        checks_by_audit = {
            aid: {c.get("code"): c.get("status") for c in f.result() if c.get("code")}
            for aid, f in checks_futures.items()
        }
        findings_by_audit = {aid: f.result() for aid, f in findings_futures.items()}
        sequences = sequences_future.result()
        check_defs = sorted(
            (d for d in defs_future.result() if d.get("enabled")),
            key=lambda d: d.get("sort_order", 0),
        )

    rows, segments = [], {}
    for r in rank(audits, prospects):
        segments[r.segment or "incomplete"] = segments.get(r.segment or "incomplete", 0) + 1
        findings = findings_by_audit.get(r.audit_id)
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
            "no_website": not (prospects.get(r.prospect_id) or {}).get("website_url"),
            "gate_override": (prospects.get(r.prospect_id) or {}).get("gate_override") == "pass",
        })
    return rows, segments, check_defs


def _excluded_for_batch(batch_id: str) -> list[dict[str, Any]] | None:
    """Prospects in this sweep's market that the sweep is not auditing.

    The dispatcher audits every "pass" and "review" prospect, so a "Needs
    review" prospect is normally on the call list, not here. What belongs
    here is anything with no audit task in this batch: the gate's "fail"
    verdicts, and any prospect a limit left out. Membership is decided by
    the task ledger rather than by gate result so the tab and the call list
    never overlap, which they did for a while.

    None when the sweep has no batch document or market, which is what a
    CLI-built batch looks like: the tab then says there is no gate record
    rather than claiming nothing was excluded.
    """
    from app.console import calllist
    from app.leases import tasks_for_batch

    batch = store.get_batch(batch_id) or {}
    market_id = batch.get("market_id")
    if not market_id:
        return None
    in_sweep = {str(t.get("prospect_id")) for t in tasks_for_batch(batch_id)}
    rows: list[dict[str, Any]] = []
    for prospect in store.prospects_for_market(market_id):
        pid = str(prospect.get("place_id") or "")
        if pid in in_sweep:
            continue
        if prospect.get("latest_batch_id") not in (None, batch_id):
            continue
        rows.append(prospect)
    return calllist.sort_excluded(rows)


@router.get("/batches")
async def batches_screen(request: Request, days: int = 14) -> Response:
    """Recent scans. The window is adjustable because a call list does not stop
    being useful on day fifteen, and the default used to hide older ones with
    no way to reach them from the screen."""
    days = max(1, min(days, 3650))
    return _page(views.render_batches(
        await asyncio.to_thread(store.batch_overview, days), days=days, notice=_notice(request)))


@router.get("/batches/{batch_id}")
async def batch_screen(batch_id: str, request: Request, tab: str = "all") -> Response:
    from app.console import calllist

    # The three loads are independent, so they run at once. The excluded
    # read is soft: a failure there costs the tab, not the page.
    (rows, segments, check_defs), overview, excluded = await asyncio.gather(
        asyncio.to_thread(_assemble_batch, batch_id),
        asyncio.to_thread(store.batch_overview),
        asyncio.to_thread(_soft, _excluded_for_batch, None, batch_id),
    )
    progress = next((b for b in overview if b["batch_id"] == batch_id), None)
    counts = calllist.tab_counts(segments, excluded=len(excluded) if excluded is not None else None)
    return _page(views.render_batch(batch_id, rows, segments, check_defs,
                                    csrf=csrf_token(request), progress=progress,
                                    notice=_notice(request), tab=tab, counts=counts,
                                    excluded=excluded or (), excluded_known=excluded is not None,
                                    sweep_label=views.scan_title(progress) if progress else None))


@router.post("/batches/{batch_id}/include")
async def include_prospects(batch_id: str, request: Request, ids: str = Form(""),
                            csrf: str = Form(None)) -> Response:
    """Audit prospects the gate turned away. A person overrode it; that is
    recorded on each prospect, and a dispatch job queues the audits so they
    appear on this call list as they finish. Nothing is contacted."""
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")
    wanted = [i.strip() for i in ids.split(",") if i.strip()][:100]
    back = f"/console/batches/{batch_id}?tab=excluded"
    if not wanted:
        return _redirect(_with_notice(back, "not_queued", "Select at least one prospect."))

    def mark() -> list[str]:
        rules = store.load_suppressions()
        kept = []
        for pid in wanted:
            prospect = store.get_prospect(pid) or {}
            if not prospect:
                continue
            if store.suppression_hit(rules, place_id=pid, domain=prospect.get("domain"),
                                     phone=prospect.get("gbp_phone"), email=prospect.get("owner_email")):
                continue
            store.set_gate_override(pid)
            kept.append(pid)
        return kept

    kept = await asyncio.to_thread(mark)
    if not kept:
        return _redirect(_with_notice(back, "not_queued",
                                      "Every selected prospect is suppressed or unknown."))
    label = f"Audit {len(kept)} excluded prospect{'s' if len(kept) != 1 else ''}"
    job_id = await asyncio.to_thread(jobs.create, jobs.KIND_DISPATCH,
                                     {"batch_id": batch_id, "prospect_ids": kept}, label=label)
    try:
        await asyncio.to_thread(publish_job, job_id, jobs.KIND_DISPATCH)
    except Exception as exc:  # noqa: BLE001 - a job nobody will run must say so
        await asyncio.to_thread(jobs.fail, job_id, f"could not queue: {exc}")
        return _redirect(_with_notice(back, "not_queued", f"The job could not be queued: {exc}"))
    return _redirect(_with_notice(
        back, "queued",
        f"{len(kept)} audit{'s' if len(kept) != 1 else ''} queued. They appear on the call list "
        f"as they finish, usually within a few minutes."))


@router.get("/batches/{batch_id}/export.csv")
async def export_batch(batch_id: str, request: Request, tab: str = "all", q: str = "",
                       check: str = "", status: str = "", ids: str = "") -> Response:
    """The call list as a spreadsheet, narrowed the way the page is.

    Same filter function as the page, so what is on screen is what downloads.
    A UTF-8 byte order mark up front so Excel reads accents; streamed row by
    row; the vocabulary is the screen's, and contact_status names no mechanism.
    """
    import csv
    import io
    from datetime import datetime, timezone

    from fastapi.responses import StreamingResponse

    from app.console import calllist

    tab = calllist.normalize_tab(tab)
    wanted = {i for i in ids.split(",") if i.strip()}
    overview = await asyncio.to_thread(store.batch_overview)
    progress = next((b for b in overview if b["batch_id"] == batch_id), None)

    if tab == "excluded":
        prospects = await asyncio.to_thread(_soft, _excluded_for_batch, None, batch_id) or []
        if q:
            needle = q.strip().lower()
            prospects = [p for p in prospects
                         if needle in f"{p.get('business_name') or ''} {p.get('city') or ''}".lower()]
        columns = calllist.CSV_EXCLUDED_COLUMNS
        records = calllist.csv_excluded_rows(prospects)
    else:
        rows, _segments, _defs = await asyncio.to_thread(_assemble_batch, batch_id)
        rows = calllist.filter_rows(rows, tab=tab, q=q, check=check, status=status)
        if wanted:
            rows = [r for r in rows if r.get("audit_id") in wanted]
        base = _report_url(request, "x")
        report_base = base[: -len("/x")] if base else ""
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        console_base = f"{scheme}://{_host(request) or request.url.netloc}"
        columns = calllist.CSV_COLUMNS
        records = calllist.csv_rows(rows, report_base=report_base, console_base=console_base)

    def stream():
        buf = io.StringIO()
        writer = csv.writer(buf)
        yield "\ufeff"
        writer.writerow(columns)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for record in records:
            writer.writerow([record.get(c, "") for c in columns])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    name = calllist.csv_filename((progress or {}).get("market"), batch_id, tab,
                                 datetime.now(timezone.utc).strftime("%Y%m%d"))
    return StreamingResponse(stream(), media_type="text/csv; charset=utf-8",
                             headers={**_HTML_HEADERS,
                                      "Content-Disposition": f'attachment; filename="{name}"'})


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


def _history_for(prospect_id: str) -> list[dict[str, Any]] | None:
    """Every finished audit of this prospect with its sweep named, or None
    when the query cannot run yet (the index is still building) so the page
    can say so instead of drawing an empty card."""
    rows = _soft(store.audits_for_prospect, None, prospect_id)
    if rows is None:
        return None
    labels: dict[str, str] = {}
    for row in rows:
        batch_id = str(row.get("batch_id") or "")
        if batch_id and batch_id not in labels:
            batch = _soft(store.get_batch, None, batch_id) or {}
            labels[batch_id] = views.scan_title({"batch_id": batch_id, "market": batch.get("label"),
                                                 "started_at": batch.get("created_at")}) \
                if batch else batch_id
        row["sweep_label"] = labels.get(batch_id, batch_id)
    return rows


@router.get("/audits/{audit_id}")
async def audit_screen(audit_id: str, request: Request) -> Response:

    def load() -> Any:
        from app.store import evidence as evidence_store

        audit = store.get_audit(audit_id)
        if audit is None:
            return None
        audit = {"audit_id": audit_id, **audit}
        pid = str(audit.get("prospect_id"))
        return (
            audit,
            store.get_prospect(pid) or {},
            store.audit_checks(audit_id),
            {d["code"]: d for d in store.all_check_defs()},
            store.get_draft_findings(audit_id),
            _evidence_with_urls(evidence_store, audit_id),
            _soft(store.get_sequence, None, pid),
            _soft(store.touches_for, [], pid),
            _soft(store.replies_for, [], pid),
            _history_for(pid),
            _soft(store.get_email_templates, None),
        )

    loaded = await asyncio.to_thread(load)
    if loaded is None:
        return Response(status_code=404)
    (audit, prospect, checks, definitions, findings, evidence,
     sequence, touches, replies, history, templates) = loaded
    return _page(views.render_audit(
        audit=audit, prospect=prospect, checks=checks, definitions=definitions,
        findings=findings, evidence=evidence, csrf=csrf_token(request), notice=_notice(request),
        sequence=sequence, touches=touches, replies=replies, history=history,
        report_url=_report_url(request, audit.get("report_slug")),
        signature=get_config().outreach_signature,
        sender_name=get_config().outreach_sender_name,
        templates=templates,
        mailbox=get_config().outreach_mailbox,
        quo_from=get_config().quo_from,
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
    target = _with_notice(back, "not_recorded", blocked) if blocked else back
    if back.startswith("/console/audits/"):
        target += "#outreach"
    return _redirect(target)


# ── Email templates ───────────────────────────────────────────────────────────


@router.get("/templates")
async def templates_screen(request: Request) -> Response:
    saved = await asyncio.to_thread(_soft, store.get_email_templates, None)
    return _page(views.render_templates(saved, csrf=csrf_token(request), notice=_notice(request)))


@router.post("/templates")
async def save_templates(request: Request, csrf: str = Form(None)) -> Response:
    """Validate and store the four templates. Sends nothing; the next draft
    on every prospect page picks these up."""
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")
    from app import outreach_templates as tpl

    form = await request.form()
    templates = tpl.normalise({k: str(v) for k, v in form.items()})
    problems = tpl.all_problems(templates)
    if problems:
        return _redirect(_with_notice("/console/templates", "templates_rejected",
                                      " ".join(problems)))
    await asyncio.to_thread(store.save_email_templates, templates)
    return _redirect(_with_notice("/console/templates", "templates_saved", ""))


# ── Sending ───────────────────────────────────────────────────────────────────


def _send_checks(prospect_id: str, prospect: Mapping[str, Any], to: str) -> str | None:
    """Everything that must be true before a byte leaves. Suppression first
    (rule 3), then the address, then the daily cap. A sentence when blocked."""
    from datetime import datetime, timezone

    hit = store.suppression_hit(
        store.load_suppressions(),
        place_id=prospect_id,
        domain=prospect.get("domain"),
        phone=prospect.get("gbp_phone"),
        email=to or prospect.get("owner_email"),
    )
    if hit:
        return f"This prospect is suppressed ({hit})."
    if not to or "@" not in to or any(c in to for c in " ,;"):
        return "The To field needs one email address."
    cap = get_config().outreach_daily_cap
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if store.daily_sends(today) >= cap:
        return (f"The daily limit of {cap} emails has been reached. It resets at midnight UTC "
                "(OUTREACH_DAILY_CAP).")
    return None


@router.post("/outreach/{prospect_id}/send")
async def send_email(prospect_id: str, request: Request, audit_id: str = Form(None),
                     to: str = Form(""), subject: str = Form(""), body: str = Form(""),
                     csrf: str = Form(None)) -> Response:
    """Send the one email on the form, now, from the operator's mailbox.

    This is the one place in the package that sends. It runs only when a
    person has pressed Send on this message after reading it, which the form's
    confirm restates. In order: CSRF, suppression, the address, the daily cap,
    the sequence state, the copy checks (internal vocabulary, forbidden
    dashes, unknown variables), then one call to gmail.send_message, then the
    ledger. A failure at any step sends nothing and says why.
    """
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")

    from app import outreach
    from app import outreach_templates as tpl
    from app.copy_rules import sanitize
    from app.report.data import forbidden_terms_in
    from app.tools import gmail

    to = (to or "").strip()

    def send() -> tuple[str | None, str]:
        prospect = store.get_prospect(prospect_id) or {}
        blocked = _send_checks(prospect_id, prospect, to)
        if blocked:
            return blocked, ""

        row = store.get_sequence(prospect_id)
        findings_doc = store.get_draft_findings(audit_id) if audit_id else None
        if row:
            seq = outreach.Sequence.from_dict(row)
        else:
            pool = len((findings_doc or {}).get("findings") or [])
            seq = outreach.open_sequence(prospect_id, audit_id=audit_id,
                                         max_touches=outreach.touches_supported(pool) or 1)
        if not seq.is_open:
            return "This outreach sequence is finished.", ""
        ordinal = seq.touch_count + 1

        cfg = get_config()
        values = tpl.values_for(ordinal=ordinal, prospect=prospect,
                                report_url=_report_url(request, (store.get_audit(audit_id) or {}).get("report_slug")) if audit_id else "",
                                findings_doc=findings_doc, sender_name=cfg.outreach_sender_name,
                                signature=cfg.outreach_signature)
        subj, unknown_s = tpl.render(tpl.clean(subject), values)
        text, unknown_b = tpl.render(tpl.clean(body), values)
        unknown = unknown_s + [u for u in unknown_b if u not in unknown_s]
        if unknown:
            return ("Not a variable: " + ", ".join("{{" + u + "}}" for u in unknown)
                    + ". Fix or remove it."), ""
        subj = " ".join(subj.split())
        text, _ = sanitize(text)
        if not subj or not text.strip():
            return "The subject and body cannot be empty.", ""
        leaked = forbidden_terms_in(subj + " " + text)
        if leaked:
            return ("The email names internal vocabulary a contractor should never read: "
                    + ", ".join(leaked) + ". Edit it and try again."), ""

        earlier = store.touches_for(prospect_id)
        first = next((t for t in earlier if t.get("thread_id")), None)
        try:
            sent = gmail.send_message(
                to=to, subject=subj, body=text,
                thread_id=(first or {}).get("thread_id") if ordinal > 1 else None,
                in_reply_to=(first or {}).get("rfc_message_id") if ordinal > 1 else None,
                logo_url=cfg.outreach_logo_url,
            )
        except gmail.GmailUnavailable as exc:
            return f"The mailbox is not connected: {exc}", ""

        sent_at = store.utcnow()
        advanced = outreach.advance(seq, sent_at=sent_at)
        store.add_touch(prospect_id, {
            "ordinal": advanced.touch_count,
            "sent_at": sent_at,
            "audit_id": audit_id or seq.audit_id,
            "channel": "email",
            "logged_via": "console",
            "sent_via": "console",
            "to": to,
            "subject": subj,
            "body": text,
            "message_id": sent.message_id,
            "thread_id": sent.thread_id,
        })
        store.save_sequence(advanced)
        from datetime import datetime, timezone
        store.bump_daily_sends(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        return None, f"Email {advanced.touch_count} of {advanced.max_touches} to {to}."

    blocked, detail = await asyncio.to_thread(send)
    back = _back(request, f"/console/audits/{audit_id}" if audit_id else "/console/batches")
    target = _with_notice(back, "not_sent" if blocked else "sent", blocked or detail)
    if back.startswith("/console/audits/"):
        target += "#outreach"
    return _redirect(target)


# ── Quo: contacts and texts ───────────────────────────────────────────────────


@router.post("/outreach/{prospect_id}/quo-contact")
async def add_quo_contact(prospect_id: str, request: Request, audit_id: str = Form(None),
                          csrf: str = Form(None)) -> Response:
    """Create (or reuse) the Quo contact for this prospect. Sends nothing."""
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")
    from app.tools import quo

    def add() -> str | None:
        prospect = store.get_prospect(prospect_id) or {}
        hit = store.suppression_hit(store.load_suppressions(), place_id=prospect_id,
                                    domain=prospect.get("domain"), phone=prospect.get("gbp_phone"),
                                    email=prospect.get("owner_email"))
        if hit:
            return f"This prospect is suppressed ({hit})."
        slug = (store.get_audit(audit_id) or {}).get("report_slug") if audit_id else None
        try:
            contact_id = quo.ensure_contact(
                {**prospect, "place_id": prospect_id},
                report_url=_report_url(request, slug) or "",
                console_url=f"{str(request.base_url).rstrip('/')}/console/audits/{audit_id}" if audit_id else "")
        except (quo.QuoUnavailable, ValueError) as exc:
            return str(exc)
        store.set_quo_contact(prospect_id, contact_id,
                              quo.e164_of(prospect.get("gbp_phone") or prospect.get("phone")))
        return None

    blocked = await asyncio.to_thread(add)
    back = _back(request, f"/console/audits/{audit_id}" if audit_id else "/console/batches")
    target = _with_notice(back, "quo_failed" if blocked else "quo_added", blocked or "")
    return _redirect(target + "#text" if back.startswith("/console/audits/") else target)


@router.post("/outreach/{prospect_id}/text")
async def send_text(prospect_id: str, request: Request, audit_id: str = Form(None),
                    to: str = Form(""), body: str = Form(""), csrf: str = Form(None)) -> Response:
    """Send the one text on the form, now, from the operator's Quo number.

    The only place that texts. Same discipline as the email route: a person
    pressed Send on this message after reading it. In order: CSRF,
    suppression (the number in +1 form too), the number, the daily text
    cap, unknown variables, internal vocabulary, the length, then one call
    to quo.send_text, then the ledger. A text is recorded as its own touch
    and does not advance the email schedule.
    """
    if not check_csrf(request, csrf):
        return Response(status_code=403, content="stale form, reload the page")

    from datetime import datetime, timezone

    from app import outreach_templates as tpl
    from app.copy_rules import sanitize
    from app.report.data import forbidden_terms_in
    from app.tools import quo

    def send() -> tuple[str | None, str]:
        prospect = store.get_prospect(prospect_id) or {}
        number = quo.e164_of(to)
        if not number:
            return "The To field needs one phone number.", ""
        rules = store.load_suppressions()
        hit = (store.suppression_hit(rules, place_id=prospect_id, domain=prospect.get("domain"),
                                     phone=prospect.get("gbp_phone"), email=prospect.get("owner_email"))
               or store.suppression_hit(rules, phone=number))
        if hit:
            return f"This prospect is suppressed ({hit}).", ""
        cfg = get_config()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if store.daily_texts(today) >= cfg.outreach_text_daily_cap:
            return (f"The daily limit of {cfg.outreach_text_daily_cap} texts has been reached. "
                    "It resets at midnight UTC (OUTREACH_TEXT_DAILY_CAP)."), ""
        findings_doc = store.get_draft_findings(audit_id) if audit_id else None
        slug = (store.get_audit(audit_id) or {}).get("report_slug") if audit_id else None
        values = tpl.values_for(ordinal=1, prospect=prospect, report_url=_report_url(request, slug) or "",
                                findings_doc=findings_doc, sender_name=cfg.outreach_sender_name,
                                signature=cfg.outreach_signature)
        text, unknown = tpl.render(tpl.clean(body), values)
        if unknown:
            return "Not a variable: " + ", ".join("{{" + u + "}}" for u in unknown) + ". Fix or remove it.", ""
        text, _ = sanitize(" ".join(text.split()))
        if not text:
            return "The text is empty.", ""
        leaked = forbidden_terms_in(text)
        if leaked:
            return ("The text names internal vocabulary a contractor should never read: "
                    + ", ".join(leaked) + ". Edit it and try again."), ""
        if len(text) > tpl.TEXT_CAP:
            return f"The text is {len(text)} characters; the limit is {tpl.TEXT_CAP}.", ""
        try:
            sent = quo.send_text(to=number, content=text)
        except (quo.QuoUnavailable, ValueError) as exc:
            return f"Quo did not send it: {exc}", ""
        sent_at = store.utcnow()
        store.add_touch(prospect_id, {
            "channel": "sms", "sent_at": sent_at, "audit_id": audit_id, "logged_via": "console",
            "sent_via": "console", "to": number, "body": text,
            "message_id": sent.message_id, "resource_id": sent.message_id,
            "conversation_id": sent.conversation_id,
        })
        if not store.get_sequence(prospect_id):
            pool = len((findings_doc or {}).get("findings") or [])
            from app import outreach
            store.save_sequence(outreach.open_sequence(prospect_id, audit_id=audit_id,
                                                       max_touches=outreach.touches_supported(pool) or 1))
        store.bump_daily_texts(today)
        return None, f"Text to {number}."

    blocked, detail = await asyncio.to_thread(send)
    back = _back(request, f"/console/audits/{audit_id}" if audit_id else "/console/batches")
    target = _with_notice(back, "text_not_sent" if blocked else "text_sent", blocked or detail)
    return _redirect(target + "#text" if back.startswith("/console/audits/") else target)


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
