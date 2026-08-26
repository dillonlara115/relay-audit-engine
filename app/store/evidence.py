"""Evidence in Cloud Storage. Screenshots and raw payloads live here, not in
Firestore, whose 1 MiB document ceiling a single screenshot walks past.

Signed URLs are minted through the IAM credentials API rather than a key file:
the runtime identity signs as itself, which is the no-keys-anywhere rule from
the engine spec holding at the last place it usually breaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Any

import google.auth
from google.auth.transport import requests as ga_requests
from google.cloud import storage

from app.config import get_config
from app.store import firestore as store

# The engine spec asks for 30 day expiry. V4 signing without a key file caps
# at 7 days, and keyless signing is the harder rule to keep, so 7 wins. The
# spec also wants URLs regenerated per page load, which makes the shorter
# expiry invisible to a reader.
SIGNED_URL_DAYS = 7


@lru_cache(maxsize=1)
def _client() -> storage.Client:
    cfg = get_config()
    cfg.require("project")
    return storage.Client(project=cfg.project)


@lru_cache(maxsize=1)
def _signer() -> tuple[str, Any]:
    """The service account email to sign as, plus refreshed credentials.

    On Cloud Run the ambient identity is the worker SA. On a workstation the
    ADC are user credentials that cannot sign at all, so signing is delegated
    to the worker SA through the IAM API, which the operator was granted.
    """
    credentials, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(ga_requests.Request())
    email = getattr(credentials, "service_account_email", None)
    if not email or email == "default":
        email = f"relay-worker@{get_config().project}.iam.gserviceaccount.com"
    return email, credentials


@dataclass(frozen=True)
class EvidenceRef:
    gcs_path: str
    kind: str
    code: str


def evidence_path(prospect_id: str, audit_id: str, name: str) -> str:
    return f"evidence/{prospect_id}/{audit_id}/{name}"


def upload(
    prospect_id: str,
    audit_id: str,
    name: str,
    payload: bytes,
    *,
    content_type: str,
    code: str,
    kind: str,
) -> EvidenceRef:
    """Store one artifact and record it under the audit. Returns the reference."""
    cfg = get_config()
    cfg.require("gcs_evidence_bucket")
    path = evidence_path(prospect_id, audit_id, name)
    bucket = _client().bucket(cfg.gcs_evidence_bucket)
    bucket.blob(path).upload_from_string(payload, content_type=content_type)

    store.get_client().collection(store.AUDITS).document(audit_id).collection(
        store.EVIDENCE
    ).document(name.replace("/", "_")).set(
        {
            "code": code,
            "kind": kind,
            "gcs_path": path,
            "content_type": content_type,
            "size_bytes": len(payload),
            "captured_at": store.utcnow(),
        }
    )
    return EvidenceRef(gcs_path=path, kind=kind, code=code)


def signed_url(gcs_path: str, *, days: int = SIGNED_URL_DAYS) -> str:
    """A time-boxed public URL for one artifact, regenerated per page load."""
    cfg = get_config()
    email, credentials = _signer()
    blob = _client().bucket(cfg.gcs_evidence_bucket).blob(gcs_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(days=min(days, SIGNED_URL_DAYS)),
        service_account_email=email,
        access_token=credentials.token,
        method="GET",
    )


def audit_evidence(audit_id: str) -> list[dict[str, Any]]:
    parent = store.get_client().collection(store.AUDITS).document(audit_id).collection(
        store.EVIDENCE
    )
    return [snap.to_dict() or {} for snap in parent.stream()]
