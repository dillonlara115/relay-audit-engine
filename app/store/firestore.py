"""Firestore access. Every collection in the data model lives behind this module.

Nothing else in the codebase constructs a Firestore client or spells a collection
name. That keeps the composite-index requirements in one place and makes the
eventual move off Firestore a single-file problem.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable, Iterator, Mapping

from google.cloud import firestore

from app.config import get_config

# ── Collection names ──────────────────────────────────────────────────────────
MARKETS = "markets"
BATCHES = "batches"
PROSPECTS = "prospects"
AUDITS = "audits"
CHECKS = "checks"  # subcollection of audits
EVIDENCE = "evidence"  # subcollection of audits
REPORT_FINDINGS = "report_findings"
CHECK_DEFS = "check_defs"
SUPPRESSIONS = "suppressions"
OUTREACH = "outreach"
API_CACHE = "api_cache"

GATE_PASS = "pass"
GATE_REVIEW = "review"
GATE_FAIL = "fail"

CACHE_TTL_DAYS = {"places": 30, "psi": 3, "serp": 7, "ad_library": 7}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def get_client() -> firestore.Client:
    cfg = get_config()
    cfg.require("project")
    return firestore.Client(project=cfg.project, database=cfg.firestore_database)


def _plain(value: Any) -> Any:
    """Make a value Firestore-safe without inventing anything.

    Keys whose value is None are dropped, not stored as null. Guardrail 4:
    unknown means the field is absent.
    """
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value if v is not None]
    return value


# ── Markets ───────────────────────────────────────────────────────────────────


def market_id_for(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.strip().lower()).strip("-")


def upsert_market(
    name: str,
    *,
    center_lat: float | None = None,
    center_lng: float | None = None,
    radius_meters: int | None = None,
    active: bool = True,
) -> str:
    market_id = market_id_for(name)
    doc = get_client().collection(MARKETS).document(market_id)
    doc.set(
        _plain(
            {
                "name": name,
                "center_lat": center_lat,
                "center_lng": center_lng,
                "radius_meters": radius_meters,
                "active": active,
                "updated_at": utcnow(),
            }
        ),
        merge=True,
    )
    return market_id


# ── Batches ───────────────────────────────────────────────────────────────────


def create_batch(market_id: str, label: str) -> str:
    doc = get_client().collection(BATCHES).document()
    doc.set(
        {
            "market_id": market_id,
            "label": label,
            "status": "running",
            "counts": {"ingested": 0, "gated": 0, "audited": 0, "failed": 0},
            "created_at": utcnow(),
        }
    )
    return doc.id


def bump_batch_counts(batch_id: str, **deltas: int) -> None:
    """Increment counters, creating the batch doc if it does not exist.

    update() raises NotFound on a missing doc, and a dispatched batch id has no
    doc unless a sweep created one. That NotFound surfaced after an audit had
    already completed, turning a finished prospect into a 500 and a redelivery.
    set(merge) with Increment does the same arithmetic without the precondition.
    """
    updates = {"counts": {k: firestore.Increment(v) for k, v in deltas.items() if v}}
    if updates["counts"]:
        get_client().collection(BATCHES).document(batch_id).set(updates, merge=True)


def complete_batch(batch_id: str, status: str = "complete") -> None:
    get_client().collection(BATCHES).document(batch_id).update(
        {"status": status, "completed_at": utcnow()}
    )


def get_batch(batch_id: str) -> dict[str, Any] | None:
    snap = get_client().collection(BATCHES).document(batch_id).get()
    return snap.to_dict() if snap.exists else None


# ── Prospects ─────────────────────────────────────────────────────────────────


def upsert_prospect(place_id: str, fields: Mapping[str, Any]) -> str:
    """Upsert on place_id, which is the document id. Never clobbers with nulls."""
    doc = get_client().collection(PROSPECTS).document(place_id)
    payload = dict(_plain(fields))
    payload["place_id"] = place_id
    payload["updated_at"] = utcnow()
    snapshot = doc.get()
    if not snapshot.exists:
        payload["created_at"] = utcnow()
        payload.setdefault("suppressed", False)
    doc.set(payload, merge=True)
    return place_id


def get_prospect(place_id: str) -> dict[str, Any] | None:
    snap = get_client().collection(PROSPECTS).document(place_id).get()
    return snap.to_dict() if snap.exists else None


def set_gate_result(place_id: str, result: str, reasons: list[Mapping[str, Any]]) -> None:
    get_client().collection(PROSPECTS).document(place_id).set(
        {
            "gate_result": result,
            "gate_reasons": _plain(list(reasons)),
            "gated_at": utcnow(),
            "updated_at": utcnow(),
        },
        merge=True,
    )


def mark_suppressed(place_id: str, reason: str) -> None:
    get_client().collection(PROSPECTS).document(place_id).set(
        {"suppressed": True, "suppressed_reason": reason, "updated_at": utcnow()},
        merge=True,
    )


def prospects_for_market(
    market_id: str,
    *,
    gate_result: str | None = None,
    suppressed: bool = False,
) -> Iterator[dict[str, Any]]:
    """Backed by composite index: market_id ASC, gate_result ASC, suppressed ASC."""
    query = (
        get_client()
        .collection(PROSPECTS)
        .where(filter=firestore.FieldFilter("market_id", "==", market_id))
        .where(filter=firestore.FieldFilter("suppressed", "==", suppressed))
    )
    if gate_result is not None:
        query = query.where(filter=firestore.FieldFilter("gate_result", "==", gate_result))
    for snap in query.stream():
        yield snap.to_dict()


def prospects_for_batch(batch_id: str) -> Iterator[dict[str, Any]]:
    query = get_client().collection(PROSPECTS).where(
        filter=firestore.FieldFilter("latest_batch_id", "==", batch_id)
    )
    for snap in query.stream():
        yield snap.to_dict()


# ── Audits ────────────────────────────────────────────────────────────────────


def create_audit(prospect_id: str, batch_id: str) -> str:
    doc = get_client().collection(AUDITS).document()
    doc.set(
        {
            "prospect_id": prospect_id,
            "batch_id": batch_id,
            "status": "queued",
            "started_at": utcnow(),
        }
    )
    get_client().collection(PROSPECTS).document(prospect_id).set(
        {"latest_audit_id": doc.id, "updated_at": utcnow()}, merge=True
    )
    return doc.id


def audits_for_batch(batch_id: str, segment: str | None = None) -> Iterator[dict[str, Any]]:
    """Every audit in a batch, or one segment of it.

    Ordering happens in the ranker, not here. An order_by("segment") looks free
    given the composite index, but Firestore excludes any document missing an
    ordered field, and an unsegmented audit stores no segment at all (absent is
    absent). The first ranked batch silently lost its 22 unsegmented audits
    that way: the call list reported 18 of 40 and nothing errored. The index
    still serves the segment-filtered form below.
    """
    query = get_client().collection(AUDITS).where(
        filter=firestore.FieldFilter("batch_id", "==", batch_id)
    )
    if segment is not None:
        query = query.where(filter=firestore.FieldFilter("segment", "==", segment))
        query = query.order_by("scores.booked")
    for snap in query.stream():
        yield {"audit_id": snap.id, **(snap.to_dict() or {})}


# ── Check definitions ─────────────────────────────────────────────────────────


def upsert_check_defs(defs: Iterable[Mapping[str, Any]]) -> int:
    client = get_client()
    batch = client.batch()
    count = 0
    for definition in defs:
        ref = client.collection(CHECK_DEFS).document(definition["code"])
        batch.set(ref, _plain(dict(definition)), merge=True)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = client.batch()
    batch.commit()
    return count


def enabled_check_defs() -> list[dict[str, Any]]:
    query = get_client().collection(CHECK_DEFS).where(
        filter=firestore.FieldFilter("enabled", "==", True)
    )
    return sorted(
        (snap.to_dict() for snap in query.stream()),
        key=lambda d: d.get("sort_order", 0),
    )


# ── Suppressions ──────────────────────────────────────────────────────────────


def add_suppression(match_type: str, match_value: str, reason: str) -> str:
    doc = get_client().collection(SUPPRESSIONS).document()
    doc.set(
        {
            "match_type": match_type,
            "match_value": match_value.strip().lower(),
            "reason": reason,
            "created_at": utcnow(),
        }
    )
    return doc.id


@lru_cache(maxsize=1)
def _suppression_cache_token() -> object:
    return object()


def load_suppressions() -> dict[str, set[str]]:
    """Load the whole suppression list. It is small and read on every action."""
    out: dict[str, set[str]] = {"place_id": set(), "domain": set(), "phone": set(), "email": set()}
    for snap in get_client().collection(SUPPRESSIONS).stream():
        row = snap.to_dict() or {}
        bucket = out.get(row.get("match_type", ""))
        if bucket is not None and row.get("match_value"):
            bucket.add(str(row["match_value"]).strip().lower())
    return out


def suppression_hit(
    suppressions: Mapping[str, set[str]],
    *,
    place_id: str | None = None,
    domain: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> str | None:
    """Return the matching rule as 'type:value', or None. Checked before every action."""
    candidates = (
        ("place_id", place_id),
        ("domain", domain),
        ("phone", phone),
        ("email", email),
    )
    for match_type, value in candidates:
        if not value:
            continue
        if value.strip().lower() in suppressions.get(match_type, set()):
            return f"{match_type}:{value.strip().lower()}"
    return None


# ── Provider cache ────────────────────────────────────────────────────────────


def cache_key(provider: str, request: Any) -> str:
    blob = json.dumps(request, sort_keys=True, default=str)
    return hashlib.sha256(f"{provider}:{blob}".encode()).hexdigest()


def cache_get(provider: str, request: Any) -> Any | None:
    key = cache_key(provider, request)
    snap = get_client().collection(API_CACHE).document(key).get()
    if not snap.exists:
        return None
    row = snap.to_dict() or {}
    expires_at = row.get("expires_at")
    if expires_at and expires_at < utcnow():
        return None
    return row.get("response")


def cache_put(provider: str, request: Any, response: Any, ttl_days: int | None = None) -> None:
    key = cache_key(provider, request)
    days = ttl_days if ttl_days is not None else CACHE_TTL_DAYS.get(provider, 7)
    get_client().collection(API_CACHE).document(key).set(
        {
            "provider": provider,
            "response": response,
            "fetched_at": utcnow(),
            "expires_at": utcnow() + timedelta(days=days),
        }
    )


def all_check_defs() -> list[dict[str, Any]]:
    """Every definition, enabled or not, in display order."""
    return sorted(
        (snap.to_dict() or {} for snap in get_client().collection(CHECK_DEFS).stream()),
        key=lambda d: d.get("sort_order", 0),
    )


def update_audit(audit_id: str, fields: Mapping[str, Any]) -> None:
    get_client().collection(AUDITS).document(audit_id).set(_plain(dict(fields)), merge=True)


def write_check_results(audit_id: str, rows: Iterable[Mapping[str, Any]]) -> int:
    """Write audits/{auditId}/checks/{code}. One batch, one round trip."""
    client = get_client()
    batch = client.batch()
    parent = client.collection(AUDITS).document(audit_id).collection(CHECKS)
    count = 0
    for row in rows:
        batch.set(parent.document(str(row["code"])), _plain(dict(row)), merge=True)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = client.batch()
    batch.commit()
    return count


def audit_checks(audit_id: str) -> list[dict[str, Any]]:
    parent = get_client().collection(AUDITS).document(audit_id).collection(CHECKS)
    return [snap.to_dict() or {} for snap in parent.stream()]


def get_audit(audit_id: str) -> dict[str, Any] | None:
    snap = get_client().collection(AUDITS).document(audit_id).get()
    return snap.to_dict() if snap.exists else None


def save_draft_findings(audit_id: str, findings: list[Mapping[str, Any]],
                        *, needs_review: bool, model: str | None) -> None:
    """Store the diagnostician's draft. Status is draft until a human approves.

    Rule 7: findings are human-selected. Nothing that reads this collection may
    publish a report from a doc whose status is not approved.
    """
    get_client().collection(REPORT_FINDINGS).document(audit_id).set(
        _plain({
            "findings": list(findings),
            "status": "draft",
            "needs_review": needs_review,
            "model": model,
            "drafted_at": utcnow(),
        })
    )


def get_draft_findings(audit_id: str) -> dict[str, Any] | None:
    snap = get_client().collection(REPORT_FINDINGS).document(audit_id).get()
    return snap.to_dict() if snap.exists else None
