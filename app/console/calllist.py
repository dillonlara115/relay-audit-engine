"""The call list's tabs and filters, as pure functions.

Shared by the page and the CSV export, so what you see is what you download.
Nothing here touches Firestore: rows come in as the plain dicts
routes._assemble_batch builds, and go out narrowed.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# Tab slug -> segment name. "all" and "excluded" are not segments.
TAB_SEGMENTS: dict[str, str] = {
    "leaky-bucket": "Leaky Bucket",
    "invisible-pro": "Invisible Pro",
    "both-broken": "Both Broken",
    "dialed": "Dialed",
    "incomplete": "incomplete",
}

TAB_ORDER: tuple[tuple[str, str], ...] = (
    ("all", "All"),
    ("leaky-bucket", "Leaky Bucket"),
    ("invisible-pro", "Invisible Pro"),
    ("both-broken", "Both Broken"),
    ("dialed", "Dialed"),
    ("incomplete", "Incomplete"),
    ("excluded", "Excluded"),
)

TAB_SLUGS = frozenset(slug for slug, _ in TAB_ORDER)


def normalize_tab(raw: str | None) -> str:
    slug = (raw or "all").strip().lower()
    return slug if slug in TAB_SLUGS else "all"


def tab_counts(segments: Mapping[str, int], *, excluded: int | None = None) -> dict[str, int]:
    """How many rows each tab would show. Excluded is absent until a caller
    can count it, so the tab is not drawn with a number it cannot back."""
    counts = {slug: int(segments.get(name, 0) or 0) for slug, name in TAB_SEGMENTS.items()}
    counts["all"] = sum(counts.values())
    if excluded is not None:
        counts["excluded"] = int(excluded)
    return counts


def _matches_check(row: Mapping[str, Any], check: str, status: str) -> bool:
    checks = row.get("checks") or {}
    if check not in checks:
        return False
    return not status or checks.get(check) == status


def filter_rows(rows: Iterable[Mapping[str, Any]], *, tab: str = "all", q: str = "",
                check: str = "", status: str = "") -> list[Mapping[str, Any]]:
    """Narrow the ranked rows the way the page does, server side.

    The page filters by search and check in the browser as well, over rows
    already present, so nothing round-trips on a keystroke. This is the same
    rule in Python so the export can apply it.
    """
    tab = normalize_tab(tab)
    needle = (q or "").strip().lower()
    out: list[Mapping[str, Any]] = []
    for row in rows:
        segment = row.get("segment") or "incomplete"
        if tab in TAB_SEGMENTS and segment != TAB_SEGMENTS[tab]:
            continue
        if needle:
            haystack = f"{row.get('business_name') or ''} {row.get('city') or ''}".lower()
            if needle not in haystack:
                continue
        if check and not _matches_check(row, check, status):
            continue
        out.append(row)
    return out


def findings_state(row: Mapping[str, Any]) -> tuple[str, str]:
    """One pill for where a prospect's findings and report are."""
    if row.get("report_slug"):
        return "ok", "Published"
    status = row.get("findings_status")
    if status == "approved":
        return "ok", "Approved"
    if status == "draft":
        return "tint", "Draft"
    return "dim", "Not drafted"


TAG_TITLES = {
    "Partial": ("Partial audit: not enough checks finished in one or more sections to "
                "score it fairly, usually because the site blocked the crawl or a page "
                "timed out. The scores may read low. Re-audit before trusting them."),
    "Agency": ("An agency already runs this site: its footer credits one. Expect a "
               "harder sell and a slower switch."),
}


def row_tags(row: Mapping[str, Any]) -> list[str]:
    tags = []
    if row.get("incumbent_agency"):
        tags.append("Agency")
    if row.get("partial"):
        tags.append("Partial")
    return tags


def visible_tabs(counts: Mapping[str, int]) -> Sequence[tuple[str, str, int]]:
    """Tabs in order with their counts; Excluded only when it has been counted."""
    return [(slug, label, counts.get(slug, 0)) for slug, label in TAB_ORDER
            if slug != "excluded" or "excluded" in counts]


# ── The Excluded tab ──────────────────────────────────────────────────────────

GATE_LABEL = {"fail": ("bad", "Excluded"), "review": ("warn", "Needs review")}


def excluded_rows_vm(prospects: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Gated-out prospects for the Excluded table: who, how to reach them, why."""
    out = []
    for p in prospects:
        reasons = [r for r in (p.get("gate_reasons") or []) if isinstance(r, Mapping)]
        failed = [str(r.get("label") or r.get("code") or "") for r in reasons
                  if r.get("verdict") == "fail"]
        advisory = [str(r.get("label") or r.get("code") or "") for r in reasons
                    if r.get("verdict") not in ("fail", "pass")]
        detail = "; ".join(f"{r.get('label') or r.get('code')}: {r.get('detail')}"
                           for r in reasons if r.get("detail"))
        kind, label = GATE_LABEL.get(str(p.get("gate_result") or ""), ("dim", "Unknown"))
        out.append({
            "prospect_id": p.get("place_id") or "",
            "business_name": p.get("business_name") or "",
            "city": p.get("city") or "",
            "phone": p.get("gbp_phone") or p.get("site_phone") or "",
            "website": p.get("website_url") or "",
            "domain": p.get("domain") or "",
            "gate": (kind, label),
            "reasons": "; ".join(failed or advisory) or "No reason recorded",
            "detail": detail,
            "maps_uri": p.get("maps_uri") or "",
            "needle": f"{p.get('business_name') or ''} {p.get('city') or ''}".lower(),
        })
    return out


def sort_excluded(prospects: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Needs review first, since those are the ones a person can act on."""
    return sorted(prospects, key=lambda p: (p.get("gate_result") != "review",
                                            (p.get("business_name") or "").lower()))


# ── CSV export ────────────────────────────────────────────────────────────────

CSV_COLUMNS = ("rank", "prospect", "city", "segment", "found", "chosen", "booked", "total",
               "phone", "contact_email", "contact_status", "outreach", "emails_sent",
               "next_due", "findings", "report_url", "tags", "audit_url")
CSV_EXCLUDED_COLUMNS = ("prospect", "city", "phone", "website", "gate", "reasons",
                        "google_profile")

_CONTACT_WORDS = {"valid": "Good", "risky": "Check first", "unknown": "Unchecked"}


def _first_contact(row: Mapping[str, Any]) -> tuple[str, str]:
    for c in row.get("contacts") or []:
        if c.get("status") in _CONTACT_WORDS:
            return str(c.get("email") or ""), _CONTACT_WORDS[str(c.get("status"))]
    return "", ""


def csv_rows(rows: Iterable[Mapping[str, Any]], *, report_base: str,
             console_base: str) -> list[dict[str, Any]]:
    """The call list as it reads on screen, one dict per row in CSV_COLUMNS."""
    from app.console.views import outreach_state

    out = []
    for r in rows:
        scores = r.get("scores") or {}
        email, contact_status = _first_contact(r)
        seq = r.get("sequence") or {}
        due = seq.get("next_due_at")
        segment = r.get("segment") or "incomplete"
        out.append({
            "rank": r.get("rank", ""),
            "prospect": r.get("business_name") or "",
            "city": r.get("city") or "",
            "segment": "Incomplete" if segment == "incomplete" else segment,
            "found": scores.get("found", ""), "chosen": scores.get("chosen", ""),
            "booked": scores.get("booked", ""), "total": scores.get("total", ""),
            "phone": r.get("phone") or "",
            "contact_email": email, "contact_status": contact_status,
            "outreach": outreach_state(r.get("sequence"), can_start=bool(r.get("report_slug")))[1],
            "emails_sent": seq.get("touch_count", 0) if seq else 0,
            "next_due": due.strftime("%Y-%m-%d") if hasattr(due, "strftime") else "",
            "findings": findings_state(r)[1],
            "report_url": f"{report_base}/{r['report_slug']}" if r.get("report_slug") else "",
            "tags": "; ".join(row_tags(r)),
            "audit_url": f"{console_base}/console/audits/{r.get('audit_id') or ''}",
        })
    return out


def csv_excluded_rows(prospects: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "prospect": v["business_name"], "city": v["city"], "phone": v["phone"],
        "website": v["website"], "gate": v["gate"][1], "reasons": v["reasons"],
        "google_profile": v["maps_uri"],
    } for v in excluded_rows_vm(prospects)]


def csv_filename(market: str | None, batch_id: str, tab: str, today: str) -> str:
    import re as _re

    base = _re.sub(r"[^a-z0-9]+", "-", (market or "").lower()).strip("-") or batch_id
    return f"call-list-{base}-{today}-{normalize_tab(tab)}.csv"
