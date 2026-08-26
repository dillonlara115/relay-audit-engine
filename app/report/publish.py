"""Publishing a report. The gate where every hard rule gets checked last.

Publish fails loudly rather than shipping a page that breaks a rule:
- the findings must have been approved by a human (rule 7)
- exactly three of them (enforced again by the DTO)
- evidence must exist in storage for the audit (guardrail 10)
- suppression is checked immediately before publishing (rule 3)
- no em-dash anywhere in the rendered page (copy rule, tested and rechecked)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.copy_rules import contains_forbidden_dash
from app.report.data import PublicFinding, PublicReport, new_slug
from app.report.template import render_report
from app.store import evidence as evidence_store
from app.store import firestore as store


class PublishBlocked(RuntimeError):
    """The report cannot ship, and the message says exactly why."""


def build_public_report(
    audit: Mapping[str, Any],
    prospect: Mapping[str, Any],
    findings_doc: Mapping[str, Any],
    *,
    slug: str,
    screenshot_url: str | None = None,
) -> PublicReport:
    findings = tuple(
        PublicFinding(
            ordinal=int(row.get("ordinal") or i + 1),
            what_we_saw=str(row.get("what_we_saw") or ""),
            what_it_means=str(row.get("what_it_means") or ""),
            what_fixing_takes=str(row.get("what_fixing_takes") or ""),
        )
        for i, row in enumerate(findings_doc.get("findings") or [])
    )
    return PublicReport(
        slug=slug,
        business_name=str(prospect.get("business_name") or "your business"),
        city=str(prospect.get("city") or ""),
        findings=findings,
        screenshot_url=screenshot_url,
        competitor_note=findings_doc.get("competitor_note"),
    )


@dataclass(frozen=True)
class PublishResult:
    slug: str
    audit_id: str
    url_path: str


def publish(audit_id: str) -> PublishResult:
    """Assign a slug and mark the audit published. Raises PublishBlocked."""
    audit = store.get_audit(audit_id)
    if audit is None:
        raise PublishBlocked(f"no audit {audit_id}")

    prospect = store.get_prospect(str(audit.get("prospect_id")))
    if prospect is None:
        raise PublishBlocked("the audit's prospect no longer exists")

    # Rule 3: suppression before every outreach action, and a public report is
    # one. Checked at the last moment, not at draft time.
    hit = store.suppression_hit(
        store.load_suppressions(),
        place_id=str(audit.get("prospect_id")),
        domain=prospect.get("domain"),
        phone=prospect.get("gbp_phone"),
        email=prospect.get("owner_email"),
    )
    if hit:
        raise PublishBlocked(f"prospect is suppressed ({hit})")

    findings_doc = store.get_draft_findings(audit_id)
    if findings_doc is None:
        raise PublishBlocked("no findings drafted for this audit")
    if findings_doc.get("status") != "approved":
        raise PublishBlocked(
            "findings are not approved. A human approves them first: "
            f"status is {findings_doc.get('status')!r}"
        )

    # Guardrail 10: every finding shown to a prospect has stored evidence.
    evidence = evidence_store.audit_evidence(audit_id)
    if not evidence:
        raise PublishBlocked("no stored evidence for this audit, nothing may be claimed")

    slug = audit.get("report_slug") or new_slug()

    # The dash rule is asserted against the final rendered page, screenshots
    # and all, because that is the artifact a contractor actually reads.
    report = build_public_report(audit, prospect, findings_doc, slug=slug)
    page = render_report(report)
    if contains_forbidden_dash(page):
        raise PublishBlocked("the rendered page contains a forbidden dash")
    from app.report.data import forbidden_terms_in

    leaked = forbidden_terms_in(page)
    if leaked:
        raise PublishBlocked(f"the rendered page leaks internal vocabulary: {leaked}")

    store.update_audit(audit_id, {
        "report_slug": slug,
        "published_at": store.utcnow(),
    })
    return PublishResult(slug=slug, audit_id=audit_id, url_path=f"/r/{slug}")


def load_by_slug(slug: str) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]] | None:
    """Audit, prospect and findings for a published slug, or None."""
    from google.cloud import firestore as gcf

    snaps = list(
        store.get_client().collection(store.AUDITS)
        .where(filter=gcf.FieldFilter("report_slug", "==", slug))
        .limit(1)
        .stream()
    )
    if not snaps:
        return None
    audit = {"audit_id": snaps[0].id, **(snaps[0].to_dict() or {})}
    if not audit.get("published_at"):
        return None
    prospect = store.get_prospect(str(audit.get("prospect_id"))) or {}
    findings = store.get_draft_findings(audit["audit_id"]) or {}
    if findings.get("status") != "approved":
        return None
    return audit, prospect, findings


def render_by_slug(slug: str) -> str | None:
    """The public page, with evidence URLs minted fresh for this load."""
    loaded = load_by_slug(slug)
    if loaded is None:
        return None
    audit, prospect, findings = loaded

    screenshot_url = None
    for row in evidence_store.audit_evidence(audit["audit_id"]):
        if row.get("kind") == "screenshot" and row.get("gcs_path"):
            try:
                screenshot_url = evidence_store.signed_url(row["gcs_path"])
            except Exception:  # noqa: BLE001 - a page without its screenshot still loads
                screenshot_url = None
            break

    report = build_public_report(
        audit, prospect, findings, slug=slug, screenshot_url=screenshot_url
    )
    return render_report(report)


def log_view(slug: str, raw_ip: str, user_agent: str | None) -> None:
    """One row per view. The IP is hashed with the server salt, never stored."""
    from app.config import get_config
    from app.report.data import hash_ip

    cfg = get_config()
    store.get_client().collection("report_views").document().set({
        "slug": slug,
        "ip_hash": hash_ip(raw_ip or "unknown", cfg.report_ip_salt),
        "user_agent": (user_agent or "")[:200],
        "viewed_at": store.utcnow(),
    })
