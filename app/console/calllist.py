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
