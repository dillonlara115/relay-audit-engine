"""The ranker. Sorts a batch into the order the phone should be worked.

Pure over audit rows. Segment priority first, then Booked ascending within a
segment, per the engine spec: the emptier the bucket, the better the call.
Score-ranking alone would bury the Leaky Bucket prospects mid-list, and they
are the entire point.

Unsegmented audits sort after every named segment. An audit whose Booked
section is partial has no segment on purpose, and inventing a rank for it
would be inventing a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from app.scoring import SEGMENT_PRIORITY, UNSEGMENTED_PRIORITY


@dataclass(frozen=True)
class CallListRow:
    rank: int
    prospect_id: str
    audit_id: str
    business_name: str
    segment: str | None
    band: str | None
    scores: Mapping[str, Any] = field(default_factory=dict)
    partial: bool = False
    incumbent_agency: str | None = None
    phone: str | None = None
    city: str | None = None

    @property
    def priority(self) -> int:
        return SEGMENT_PRIORITY.get(self.segment or "", UNSEGMENTED_PRIORITY)


def _sort_key(audit: Mapping[str, Any]) -> tuple:
    scores = audit.get("scores") or {}
    segment = audit.get("segment")
    return (
        SEGMENT_PRIORITY.get(segment or "", UNSEGMENTED_PRIORITY),
        scores.get("booked", 0),          # emptier bucket first
        -(scores.get("found", 0)),        # then the most findable
        audit.get("prospect_id") or "",   # stable
    )


def rank(
    audits: list[Mapping[str, Any]],
    prospects: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[CallListRow]:
    """Audits in, call order out. Suppressed prospects never appear."""
    prospects = prospects or {}
    rows: list[CallListRow] = []

    for audit in sorted(audits, key=_sort_key):
        prospect_id = str(audit.get("prospect_id") or "")
        prospect = prospects.get(prospect_id) or {}
        if prospect.get("suppressed"):
            continue
        rows.append(
            CallListRow(
                rank=len(rows) + 1,
                prospect_id=prospect_id,
                audit_id=str(audit.get("audit_id") or ""),
                business_name=str(prospect.get("business_name")
                                  or audit.get("business_name") or prospect_id),
                segment=audit.get("segment"),
                band=audit.get("band"),
                scores=audit.get("scores") or {},
                partial=bool(audit.get("partial")),
                incumbent_agency=prospect.get("incumbent_agency"),
                phone=prospect.get("gbp_phone"),
                city=prospect.get("city"),
            )
        )
    return rows


def by_segment(rows: list[CallListRow]) -> Iterator[tuple[str, list[CallListRow]]]:
    """Grouped in priority order, unsegmented last as 'incomplete'."""
    buckets: dict[str, list[CallListRow]] = {}
    for row in rows:
        buckets.setdefault(row.segment or "incomplete", []).append(row)
    ordered = sorted(buckets.items(),
                     key=lambda kv: SEGMENT_PRIORITY.get(kv[0], UNSEGMENTED_PRIORITY))
    yield from ordered
