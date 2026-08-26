"""The ranker. Segment priority beats raw score, which is the whole argument."""

from __future__ import annotations

from app.ranker import by_segment, rank
from app.scoring import SEG_BOTH_BROKEN, SEG_DIALED, SEG_INVISIBLE_PRO, SEG_LEAKY_BUCKET


def audit(pid, segment, *, booked=10, found=25, total=50, partial=False):
    return {"prospect_id": pid, "audit_id": f"a-{pid}", "segment": segment,
            "band": "Leaking", "partial": partial,
            "scores": {"found": found, "chosen": 10, "booked": booked, "total": total}}


def test_leaky_bucket_outranks_a_higher_scoring_dialed():
    """The mid-scoring Leaky Bucket is the best call in the batch. Score
    ranking would bury him, which is why the ranker exists."""
    rows = rank([
        audit("dialed", SEG_DIALED, booked=38, total=90),
        audit("leaky", SEG_LEAKY_BUCKET, booked=12, total=48),
    ])
    assert [r.prospect_id for r in rows] == ["leaky", "dialed"]


def test_segment_order_is_the_criteria_docs_order():
    rows = rank([
        audit("d", SEG_DIALED), audit("bb", SEG_BOTH_BROKEN),
        audit("ip", SEG_INVISIBLE_PRO), audit("lb", SEG_LEAKY_BUCKET),
    ])
    assert [r.prospect_id for r in rows] == ["lb", "ip", "bb", "d"]


def test_within_a_segment_the_emptier_bucket_calls_first():
    rows = rank([
        audit("fuller", SEG_LEAKY_BUCKET, booked=18),
        audit("emptier", SEG_LEAKY_BUCKET, booked=4),
    ])
    assert [r.prospect_id for r in rows] == ["emptier", "fuller"]


def test_unsegmented_sorts_dead_last():
    rows = rank([
        audit("incomplete", None, booked=0, partial=True),
        audit("d", SEG_DIALED, booked=40),
    ])
    assert rows[-1].prospect_id == "incomplete"
    assert rows[-1].segment is None


def test_suppressed_prospects_never_appear():
    """Suppression is checked before every outreach action. A call list is one."""
    rows = rank(
        [audit("ok", SEG_LEAKY_BUCKET), audit("gone", SEG_LEAKY_BUCKET)],
        prospects={"gone": {"suppressed": True, "business_name": "Gone Roofing"},
                   "ok": {"business_name": "OK Roofing"}},
    )
    assert [r.prospect_id for r in rows] == ["ok"]


def test_ranks_are_dense_after_suppression():
    rows = rank(
        [audit("a", SEG_DIALED), audit("b", SEG_DIALED), audit("c", SEG_DIALED)],
        prospects={"b": {"suppressed": True}},
    )
    assert [r.rank for r in rows] == [1, 2]


def test_grouping_puts_incomplete_last():
    rows = rank([audit("x", None, partial=True), audit("y", SEG_LEAKY_BUCKET)])
    groups = list(by_segment(rows))
    assert [g[0] for g in groups] == [SEG_LEAKY_BUCKET, "incomplete"]


def test_stable_order_for_identical_shapes():
    one = rank([audit("b", SEG_DIALED), audit("a", SEG_DIALED)])
    two = rank([audit("a", SEG_DIALED), audit("b", SEG_DIALED)])
    assert [r.prospect_id for r in one] == [r.prospect_id for r in two]
