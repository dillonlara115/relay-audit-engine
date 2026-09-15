"""The outreach sequence state machine. Pure, so every case is a dataclass in
and a dataclass out.

Cadence comes from criteria section 6: touches at day 0, 3, 7 and 14, then
silence. The reply cases are exercised against explicit policies rather than
`policy_for`, which is a business decision that is deliberately still open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.outreach import (
    ACTIVE,
    CLOSED,
    INTERESTED,
    MAX_TOUCHES,
    MAYBE_LATER,
    NOT_INTERESTED,
    OTHER,
    OUT_OF_OFFICE,
    PENDING,
    TOUCH_GAPS,
    WAITING,
    WRONG_PERSON,
    ReplyPolicy,
    Sequence,
    advance,
    apply_policy,
    due_after,
    open_sequence,
    finding_for_touch,
    park_reason,
    policy_for,
    record_reply,
    resume,
    touches_supported,
)

DAY0 = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)


def days(n: int) -> timedelta:
    return timedelta(days=n)


def sent_through(n: int, start: datetime = DAY0) -> Sequence:
    """A sequence with n touches sent on the intended schedule."""
    seq = open_sequence("place-1", audit_id="audit-1", now=start)
    at = start
    for _ in range(n):
        seq = advance(seq, sent_at=at)
        if seq.next_due_at:
            at = seq.next_due_at
    return seq


# ── cadence ───────────────────────────────────────────────────────────────────


def test_the_cadence_is_day_0_3_7_14():
    assert TOUCH_GAPS == (3, 4, 7)
    assert MAX_TOUCHES == 4


def test_a_new_sequence_is_pending_and_due_now():
    seq = open_sequence("place-1", now=DAY0)
    assert seq.status == PENDING
    assert seq.touch_count == 0
    assert seq.due(now=DAY0) is True


def test_each_touch_schedules_the_next_one():
    seq = advance(open_sequence("place-1", now=DAY0), sent_at=DAY0)
    assert (seq.status, seq.touch_count) == (ACTIVE, 1)
    assert seq.next_due_at == DAY0 + days(3)

    seq = advance(seq, sent_at=seq.next_due_at)
    assert seq.next_due_at == DAY0 + days(7)

    seq = advance(seq, sent_at=seq.next_due_at)
    assert seq.next_due_at == DAY0 + days(14)


def test_the_fourth_touch_closes_the_sequence():
    seq = sent_through(4)
    assert seq.status == CLOSED
    assert seq.touch_count == 4
    assert seq.next_due_at is None
    assert "no reply" in seq.closed_reason


def test_a_closed_sequence_ignores_further_touches():
    seq = sent_through(4)
    assert advance(seq, sent_at=DAY0 + days(30)) == seq


def test_a_late_touch_moves_the_rest_of_the_sequence_with_it():
    """Sending touch two a week late must not bunch three and four together."""
    seq = advance(open_sequence("place-1", now=DAY0), sent_at=DAY0)
    late = DAY0 + days(10)
    seq = advance(seq, sent_at=late)
    assert seq.next_due_at == late + days(4)


def test_a_sequence_is_not_due_before_its_date():
    seq = advance(open_sequence("place-1", now=DAY0), sent_at=DAY0)
    assert seq.due(now=DAY0 + days(2)) is False
    assert seq.due(now=DAY0 + days(3)) is True


def test_a_closed_sequence_is_never_due():
    assert sent_through(4).due(now=DAY0 + days(365)) is False


def test_due_after_is_none_past_the_last_touch():
    assert due_after(MAX_TOUCHES, DAY0) is None
    assert due_after(0, DAY0) is None


def test_touches_left_counts_down():
    assert open_sequence("place-1").touches_left == 4
    assert sent_through(2).touches_left == 2
    assert sent_through(4).touches_left == 0


# ── replies ───────────────────────────────────────────────────────────────────


def test_a_closing_reply_stops_the_clock():
    seq = apply_policy(sent_through(1), INTERESTED, ReplyPolicy(close=True), at=DAY0 + days(1))
    assert seq.status == CLOSED
    assert seq.next_due_at is None
    assert seq.last_intent == INTERESTED


def test_a_suppressing_reply_closes_and_says_so():
    seq = apply_policy(sent_through(1), NOT_INTERESTED,
                       ReplyPolicy(close=True, suppress=True), at=DAY0 + days(1))
    assert seq.status == CLOSED
    assert seq.closed_reason == "suppressed on reply"


def test_a_reply_that_does_not_burn_a_touch_rewinds_the_counter():
    """An out of office reached nobody, so that touch should go again."""
    before = sent_through(2)
    after = apply_policy(before, OUT_OF_OFFICE, ReplyPolicy(burn_touch=False),
                         at=DAY0 + days(4))
    assert after.touch_count == before.touch_count - 1
    assert after.status == ACTIVE


def test_the_counter_never_rewinds_below_zero():
    seq = apply_policy(open_sequence("place-1", now=DAY0), OUT_OF_OFFICE,
                       ReplyPolicy(burn_touch=False), at=DAY0)
    assert seq.touch_count == 0


def test_a_rewound_touch_can_be_resent_and_the_sequence_still_ends_at_four():
    seq = sent_through(2)
    seq = apply_policy(seq, OUT_OF_OFFICE, ReplyPolicy(burn_touch=False), at=DAY0 + days(4))
    for _ in range(seq.touches_left):
        seq = advance(seq, sent_at=seq.next_due_at or DAY0)
    assert seq.touch_count == MAX_TOUCHES
    assert seq.status == CLOSED


def test_defer_pushes_the_next_touch_out_from_the_reply():
    at = DAY0 + days(2)
    seq = apply_policy(sent_through(1), MAYBE_LATER, ReplyPolicy(defer_days=90), at=at)
    assert seq.next_due_at == at + days(90)
    assert seq.status == ACTIVE


def test_needs_human_parks_the_sequence_without_closing_it():
    seq = apply_policy(sent_through(1), WRONG_PERSON, ReplyPolicy(needs_human=True),
                       at=DAY0 + days(1))
    assert seq.status == WAITING
    assert seq.next_due_at is None
    assert seq.due(now=DAY0 + days(365)) is False


def test_a_parked_sequence_resumes_when_a_human_supplies_a_contact():
    seq = apply_policy(sent_through(1), WRONG_PERSON, ReplyPolicy(needs_human=True),
                       at=DAY0 + days(1))
    back = resume(seq, now=DAY0 + days(5))
    assert back.status == ACTIVE
    assert back.due(now=DAY0 + days(5)) is True


def test_resume_does_nothing_to_a_sequence_that_is_not_parked():
    seq = sent_through(1)
    assert resume(seq, now=DAY0) == seq


def test_suppress_beats_needs_human():
    seq = apply_policy(sent_through(1), WRONG_PERSON,
                       ReplyPolicy(suppress=True, needs_human=True), at=DAY0)
    assert seq.status == CLOSED


# ── persistence shape ─────────────────────────────────────────────────────────


def test_round_trips_through_a_dict():
    seq = sent_through(2)
    assert Sequence.from_dict(seq.to_dict()) == seq


def test_an_unsent_sequence_writes_no_null_last_sent_at():
    """Absent stays absent, so nothing downstream reads a null as a zero."""
    row = open_sequence("place-1", now=DAY0).to_dict()
    assert "last_sent_at" not in row
    assert "last_intent" not in row


# ── the policy table ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("intent", list(INTENTS := (
    "interested", "maybe_later", "wrong_person",
    "not_interested", "out_of_office", "other")))
def test_every_intent_has_a_policy(intent):
    assert policy_for(intent) is not None


def test_an_unclassifiable_reply_stops_for_a_human_rather_than_continuing():
    """The one outcome that is certainly wrong is carrying on blind."""
    seq, policy = record_reply(sent_through(1), "something nobody anticipated",
                               at=DAY0 + days(1))
    assert policy.needs_human is True
    assert policy.burn_touch is False
    assert seq.status == WAITING


def test_interested_stops_the_sequence_without_suppressing():
    """He is a call to make, not a no. Suppressing would block every later draft."""
    seq, policy = record_reply(sent_through(1), INTERESTED, at=DAY0 + days(1))
    assert seq.status == CLOSED
    assert policy.suppress is False


def test_not_interested_suppresses_permanently():
    seq, policy = record_reply(sent_through(1), NOT_INTERESTED, at=DAY0 + days(1))
    assert policy.suppress is True
    assert seq.status == CLOSED


def test_maybe_later_closes_and_schedules_a_fresh_look():
    """Resuming in ninety days would send a finding off a stale audit."""
    at = DAY0 + days(2)
    seq, policy = record_reply(sent_through(1), MAYBE_LATER, at=at)
    assert seq.status == CLOSED
    assert seq.next_due_at is None
    assert seq.revisit_at == at + days(90)


def test_a_suppressed_prospect_gets_no_revisit_date():
    """Coming back to someone who said no would be the whole point of rule 3."""
    seq, _ = record_reply(sent_through(1), NOT_INTERESTED, at=DAY0)
    assert seq.revisit_at is None


def test_wrong_person_parks_without_suppressing_the_forwarder():
    before = sent_through(2)
    seq, policy = record_reply(before, WRONG_PERSON, at=DAY0 + days(4))
    assert seq.status == WAITING
    assert policy.suppress is False
    assert seq.touch_count == before.touch_count - 1   # it reached nobody deciding
    assert park_reason(seq) == "needs a new contact"


def test_out_of_office_costs_nothing_and_waits_a_week():
    before = sent_through(2)
    at = DAY0 + days(4)
    seq, _ = record_reply(before, OUT_OF_OFFICE, at=at)
    assert seq.touch_count == before.touch_count - 1
    assert seq.next_due_at == at + days(7)
    assert seq.status == ACTIVE


def test_an_out_of_office_cannot_consume_the_sequence():
    """Four auto-responders in a row must not spend all four touches."""
    seq = sent_through(1)
    for i in range(4):
        seq, _ = record_reply(seq, OUT_OF_OFFICE, at=DAY0 + days(i + 1))
        seq = advance(seq, sent_at=seq.next_due_at)
    assert seq.touch_count == 1
    assert seq.is_open


def test_park_reason_distinguishes_the_two_ways_a_sequence_stalls():
    wrong, _ = record_reply(sent_through(1), WRONG_PERSON, at=DAY0)
    other, _ = record_reply(sent_through(1), OTHER, at=DAY0)
    assert park_reason(wrong) != park_reason(other)


# ── How far a pool carries a sequence ─────────────────────────────────────────


@pytest.mark.parametrize("pool,expected", [
    (3, 1),   # report only, nothing new to add
    (4, 2),
    (5, 3),
    (6, 4),   # the full cadence
    (9, 4),   # capped by section 6, not by material
    (2, 0),   # not even a report
])
def test_a_pool_buys_one_touch_per_finding_past_the_report(pool, expected):
    assert touches_supported(pool) == expected


def test_touch_one_is_the_report_and_carries_no_single_finding():
    assert finding_for_touch(1, 6) is None


def test_each_follow_up_takes_the_next_unchosen_finding():
    assert [finding_for_touch(t, 6) for t in (2, 3, 4)] == [4, 5, 6]


def test_a_touch_past_the_pool_has_nothing_to_carry():
    assert finding_for_touch(3, 4) is None


def test_a_thin_pool_closes_when_it_runs_out_of_material():
    """Four findings is two touches. Not a third with nothing new in it."""
    seq = open_sequence("p1", now=DAY0, max_touches=touches_supported(4))
    seq = advance(seq, sent_at=DAY0)
    assert seq.status == ACTIVE
    seq = advance(seq, sent_at=seq.next_due_at)
    assert seq.status == CLOSED
    assert seq.closed_reason == "no findings left to send"


def test_a_full_pool_still_runs_the_whole_cadence():
    seq = open_sequence("p1", now=DAY0, max_touches=touches_supported(6))
    for _ in range(4):
        seq = advance(seq, sent_at=seq.next_due_at or DAY0)
    assert seq.touch_count == 4
    assert seq.closed_reason == "sequence complete, no reply"


def test_the_two_ways_a_sequence_ends_are_distinguishable():
    """Ran out of things to say is not the same as said everything and got silence."""
    thin = open_sequence("p1", now=DAY0, max_touches=touches_supported(4))
    thin = advance(advance(thin, sent_at=DAY0), sent_at=DAY0 + days(3))
    full = sent_through(4)
    assert thin.closed_reason != full.closed_reason


def test_max_touches_survives_the_round_trip():
    seq = open_sequence("p1", now=DAY0, max_touches=2)
    assert Sequence.from_dict(seq.to_dict()).max_touches == 2


def test_a_sequence_stored_before_pools_existed_reads_as_the_full_cadence():
    assert Sequence.from_dict({"prospect_id": "p1", "status": "active"}).max_touches == 4
