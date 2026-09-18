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
import json
import re
from pathlib import Path
from urllib.parse import quote
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_config
from app.leases import worker_id
from app.store import firestore as store
from app.tasks import run_audit_task
from app.tools.pubsub import parse_push

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay.worker")

BUILD_SHA = os.getenv("BUILD_SHA", "dev")

app = FastAPI(title="relay-audit-worker")

# Pages compress about six to one; the call list is 130 KB of HTML before
# this. Applied to anything over a kilobyte, which leaves health checks alone.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=1024)

from app.console.auth import authorize as _console_authorize  # noqa: E402
from app.console.routes import router as console_router  # noqa: E402

app.include_router(console_router)


# Everything on this service needs a console session except these. The list is
# short, explicit, and deliberately deny-by-default: a route added next week is
# private until somebody adds it here on purpose, rather than public until
# somebody remembers to guard it.
#
# "Open" means "not console-gated", not "unauthenticated". The Pub/Sub and
# scheduler endpoints carry their own shared-secret check in `_authorized`,
# because a push subscription cannot log in to anything.
OPEN_PREFIXES = (
    "/r/",          # the pre-move report path, now a redirect. See REPORT_SLUG.
    "/console/login",   # where the password is entered, which cannot require one
    "/health",
    "/healthz",
    "/robots.txt",
    "/pubsub/",     # token-gated
    "/tick",        # token-gated
    "/static/",     # the logo an email's HTML part points at; a fixed whitelist of files
    "/quo/",        # signed by Quo; verified on the raw body before anything is read
)


# A report lives at the root, so the open rule for it is a shape rather than a
# prefix: anything else at the root stays closed. new_slug() is
# secrets.token_urlsafe(12), which is always exactly 16 characters from this
# alphabet, so the match is exact rather than a range. A route whose own path
# happened to match this would be public by accident, which is why
# test_no_route_can_be_mistaken_for_a_report walks the app and forbids it.
# Files an email or a public page may point at. A whitelist, not a directory
# listing: nothing else under app/static is reachable, whatever lands there.
STATIC_FILES = {"relay-mark.png": "image/png"}
STATIC_DIR = Path(__file__).resolve().parent / "static"


REPORT_SLUG = re.compile(r"^/[A-Za-z0-9_-]{16}$")


def is_open_path(path: str) -> bool:
    return path.startswith(OPEN_PREFIXES) or bool(REPORT_SLUG.match(path))


def public_hosts() -> frozenset[str]:
    """The contractor-facing hostnames.

    Separated by whitespace, commas or semicolons, all three accepted. gcloud
    splits --update-env-vars on commas itself, so a comma separated list here
    has to be escaped at the shell with its ^delimiter^ syntax every single
    deploy. Accepting spaces means the obvious thing works instead.
    """
    raw = get_config().public_report_host
    return frozenset(part.strip().lower().split(":")[0]
                     for part in re.split(r"[,;\s]+", raw) if part.strip())


def request_host(request: Request) -> str:
    """The hostname the caller actually asked for.

    X-Forwarded-Host first, because Firebase Hosting and a load balancer both
    proxy to Cloud Run with Host rewritten to the run.app name and the original
    put here. Falls back to Host for a direct hit or a Cloud Run domain mapping.
    """
    forwarded = (request.headers.get("x-forwarded-host") or "").split(",")[0]
    host = forwarded.strip() or request.headers.get("host") or ""
    return host.split(":")[0].strip().lower()


def on_public_host(request: Request) -> bool:
    """Whether this request arrived on the contractor-facing hostname.

    Both headers are caller-controlled, and that is fine here because the only
    thing spoofing one buys is a 404 where a 401 would otherwise be: it tells
    an attacker strictly less. The operator entrance is the Cloud Run URL,
    which is not in this set and therefore still answers with a login.
    """
    hosts = public_hosts()
    return bool(hosts) and request_host(request) in hosts


@app.middleware("http")
async def console_gate(request: Request, call_next):
    """Close the whole service except the few paths that must answer publicly.

    Runs before FastAPI parses a body. In a handler the check ran after form
    validation, so an unauthenticated POST got a 422 listing the fields it
    should have sent.

    Every response leaves with X-Robots-Tag whatever produced it, including
    404s, errors, and handlers written after this was. Nothing on this service
    belongs in a search index: the operator side is a private tool, and a
    report names one contractor's problems and is his to share or not.
    """
    if not is_open_path(request.url.path):
        # On the contractor-facing hostname the console does not exist. A 401
        # is a locked door, and a locked door tells whoever trimmed the URL
        # that there is a room behind it.
        if on_public_host(request):
            return Response(status_code=404,
                            headers={"X-Robots-Tag": "noindex, nofollow"})
        gate = _console_authorize(request)
        if gate is not None:
            gate.headers["X-Robots-Tag"] = "noindex, nofollow"
            return gate

    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/robots.txt", include_in_schema=False)
def robots() -> Response:
    """Nothing here is for a crawler.

    The authoritative control is the X-Robots-Tag header above, not this file:
    a disallowed URL is one a crawler will not fetch, so it never reads the
    noindex either. This is the polite signal, the header is the rule.
    """
    return Response(content="User-agent: *\nDisallow: /\n", media_type="text/plain",
                    headers={"X-Robots-Tag": "noindex, nofollow",
                             "Cache-Control": "public, max-age=86400"})

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


@app.get("/", include_in_schema=False)
def root() -> Response:
    """The bare hostname, for somebody who is signed in.

    Gated like everything else, so a logged out visitor sees the password form
    here and a contractor who trimmed his report URL finds nothing. Whoever
    gets past that typed the domain without a path and wants the console.
    """
    return RedirectResponse("/console", status_code=303)


@app.get("/health")
@app.get("/healthz")
def health() -> dict:
    return {"ok": True, "build": BUILD_SHA, "worker": WORKER}


# ── the old dashboard ─────────────────────────────────────────────────────────
#
# The read-only overview merged into the console. The paths stay so a bookmark
# keeps working, and they sit behind the same gate: a stranger gets the
# password form rather than the redirect, and on the contractor-facing
# hostname they remain a 404 like everything else that is not a report.


@app.get("/dashboard", include_in_schema=False)
def dashboard_moved() -> Response:
    return RedirectResponse("/console", status_code=301)


@app.get("/dashboard/{batch_id}", include_in_schema=False)
def dashboard_batch_moved(batch_id: str) -> Response:
    return RedirectResponse(f"/console/batches/{quote(batch_id, safe='')}", status_code=301)


@app.post("/quo/webhook")
async def quo_webhook(request: Request) -> Response:
    """Quo tells us a text arrived, a call finished, or a summary is ready.

    Verified on the raw bytes with the webhook's signing key; an unsigned or
    mis-signed delivery is refused before the body is parsed. Everything
    else is acknowledged with 200 so Quo stops retrying, including events
    about people not on any call list, which are ignored.
    """
    from app import quo_events
    from app.tools import quo

    key = get_config().quo_webhook_key
    raw = await request.body()
    if not quo.verify_signature(dict(request.headers), raw, key):
        return Response(status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return Response(status_code=400)
    try:
        outcome = await quo_events.handle(payload)
    except Exception:  # noqa: BLE001 - a bad event must not make Quo retry forever
        log.exception("quo webhook failed")
        return Response(status_code=200, content="error logged")
    log.info("quo %s %s %s", outcome.action, outcome.prospect_id, outcome.detail)
    return JSONResponse({"action": outcome.action})


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


@app.get("/r/{slug}", include_in_schema=False)
def public_report_legacy(slug: str) -> Response:
    """Reports published before they moved to the root.

    Three existed at the time of the move. A link that is already in somebody's
    inbox cannot be recalled, and one permanent redirect is cheaper than ever
    finding out the hard way which of them was shared.
    """
    if not REPORT_SLUG.match(f"/{slug}"):
        return Response(status_code=404)
    return RedirectResponse(f"/{slug}", status_code=301)


@app.get("/static/{name}")
async def static_file(name: str) -> Response:
    """The few files an email or public page embeds. Whitelisted by name."""
    from fastapi.responses import FileResponse

    kind = STATIC_FILES.get(name)
    if not kind or not (STATIC_DIR / name).is_file():
        return Response(status_code=404)
    return FileResponse(STATIC_DIR / name, media_type=kind,
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/{slug}")
def public_report(slug: str, request: Request) -> Response:
    """The one-page report, at the root of its own hostname.

    This is a single-segment catch-all, so every named single-segment route
    has to be declared above it or this would swallow it. Pinned by
    test_no_named_route_is_shadowed_by_the_report rather than by memory.

    Public by slug, and only by slug.

    The slug is 16 CSPRNG characters and the only access control this page
    has, which is why it never appears in a sitemap, a log line, or a search
    index. The engine spec's noindex lives in the meta tag and here in the
    header, and the view log stores a salted hash, never the address itself.
    """
    from app.report.publish import log_view, render_by_slug

    if not REPORT_SLUG.match(f"/{slug}"):
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
