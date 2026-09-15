"""The outreach sequence, as a pure state machine.

Criteria section 6 specifies the cadence and nothing in the codebase has ever
enforced it: four touches at day 0, 3, 7 and 14, each after the first adding
one new finding, then silence. Four touches and no answer is a no.

Nothing here sends anything, reads Firestore, or knows what a mailbox is. It
answers three questions over plain data: is this prospect due, what happens to
the sequence when a touch goes out, and what happens when one comes back. The
store layer performs the writes; `app.outreach` decides what they should be.

A touch advances only when an operator says it was sent. Publishing a report is
not sending it, and with no email API in the outreach path (hard rule 4) the
system cannot observe a send any other way.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

# ── Sequence status ───────────────────────────────────────────────────────────

PENDING = "pending"   # a contact exists, nothing has gone out
ACTIVE = "active"     # at least one touch sent, more scheduled
WAITING = "waiting"   # paused, needs a human before it can continue
CLOSED = "closed"     # no further touches, ever

SEQUENCE_STATUSES = frozenset({PENDING, ACTIVE, WAITING, CLOSED})

# ── Reply intent ──────────────────────────────────────────────────────────────

INTERESTED = "interested"
MAYBE_LATER = "maybe_later"
WRONG_PERSON = "wrong_person"
NOT_INTERESTED = "not_interested"
OUT_OF_OFFICE = "out_of_office"
OTHER = "other"

INTENTS = (INTERESTED, MAYBE_LATER, WRONG_PERSON, NOT_INTERESTED, OUT_OF_OFFICE, OTHER)

INTENT_LABELS = {
    INTERESTED: "Interested",
    MAYBE_LATER: "Maybe later",
    WRONG_PERSON: "Wrong person",
    NOT_INTERESTED: "Not interested",
    OUT_OF_OFFICE: "Out of office",
    OTHER: "Needs a read",
}

# Why a sequence is parked, shown to the operator who has to unpark it.
PARK_REASONS = {
    WRONG_PERSON: "needs a new contact",
    OTHER: "a reply to read",
}

# ── Cadence ───────────────────────────────────────────────────────────────────

# Days from the first touch, per criteria section 6.
TOUCH_OFFSETS = (0, 3, 7, 14)
MAX_TOUCHES = len(TOUCH_OFFSETS)

# Gaps between consecutive touches: 3 days, then 4, then 7. Measured from the
# last send rather than from the first, so a touch sent late moves the rest of
# the sequence with it instead of bunching them up.
TOUCH_GAPS = tuple(
    TOUCH_OFFSETS[i] - TOUCH_OFFSETS[i - 1] for i in range(1, len(TOUCH_OFFSETS))
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ReplyPolicy:
    """What a reply of a given intent does to the sequence.

    Five independent switches rather than one verb, because the interesting
    replies do more than one thing. "Forwarding this to our head of growth"
    should stop the clock without burning a touch and without suppressing
    anybody, which no single verb expresses.
    """

    close: bool = False          # end the sequence, no further touches
    suppress: bool = False       # permanent suppression, per criteria section 7
    burn_touch: bool = True      # False rewinds the counter so the touch resends
    defer_days: int | None = None  # push the next touch out by this many days
    needs_human: bool = False    # park it until a person acts
    revisit_days: int | None = None  # close now, but come back and re-audit then


@dataclass(frozen=True)
class Sequence:
    """One prospect's outreach state. Mirrors outreach/{prospect_id}."""

    prospect_id: str
    audit_id: str | None = None
    status: str = PENDING
    touch_count: int = 0
    last_sent_at: datetime | None = None
    next_due_at: datetime | None = None
    last_intent: str | None = None
    closed_reason: str | None = None
    revisit_at: datetime | None = None
    max_touches: int = MAX_TOUCHES

    @property
    def touches_left(self) -> int:
        return max(0, min(self.max_touches, MAX_TOUCHES) - self.touch_count)

    @property
    def is_open(self) -> bool:
        return self.status in (PENDING, ACTIVE)

    def due(self, now: datetime | None = None) -> bool:
        if not self.is_open or self.next_due_at is None:
            return False
        return self.next_due_at <= (now or _utcnow())

    def to_dict(self) -> dict[str, Any]:
        """Absent stays absent, so a sequence that has never been sent carries
        no null last_sent_at to be mistaken for a zero."""
        row: dict[str, Any] = {
            "prospect_id": self.prospect_id,
            "status": self.status,
            "touch_count": self.touch_count,
        }
        for key in ("audit_id", "last_sent_at", "next_due_at", "last_intent",
                    "closed_reason", "revisit_at"):
            value = getattr(self, key)
            if value is not None:
                row[key] = value
        if self.max_touches != MAX_TOUCHES:
            row["max_touches"] = self.max_touches
        return row

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Sequence":
        return cls(
            prospect_id=str(row.get("prospect_id") or ""),
            audit_id=row.get("audit_id"),
            status=str(row.get("status") or PENDING),
            touch_count=int(row.get("touch_count") or 0),
            last_sent_at=row.get("last_sent_at"),
            next_due_at=row.get("next_due_at"),
            last_intent=row.get("last_intent"),
            closed_reason=row.get("closed_reason"),
            revisit_at=row.get("revisit_at"),
            max_touches=int(row.get("max_touches") or MAX_TOUCHES),
        )


# A report carries three findings, so a pool of six supports the report plus
# three follow-ups. Fewer failures means fewer touches, not a touch with
# nothing new in it: criteria section 6 says each follow-up adds one finding.
REPORT_FINDINGS = 3


def touches_supported(pool_size: int, *, report_findings: int = REPORT_FINDINGS) -> int:
    """How far a sequence can run on a pool of this size.

    Three findings is one touch. Every finding past the third buys one more,
    up to the four the cadence allows.
    """
    if pool_size < report_findings:
        return 0
    return min(MAX_TOUCHES, 1 + (pool_size - report_findings))


def finding_for_touch(ordinal: int, pool_size: int,
                      *, report_findings: int = REPORT_FINDINGS) -> int | None:
    """Which finding in the pool a given touch carries, 1-based.

    Touch one is the report itself and carries the chosen three, so it has no
    single finding and returns None. Touch two onward takes the next unchosen
    one in rank order.
    """
    if ordinal <= 1:
        return None
    position = report_findings + (ordinal - 1)
    return position if position <= pool_size else None


def due_after(touch_count: int, last_sent_at: datetime,
              *, max_touches: int = MAX_TOUCHES) -> datetime | None:
    """When the next touch comes due, or None when the sequence is spent."""
    if touch_count < 1 or touch_count >= min(max_touches, MAX_TOUCHES):
        return None
    return last_sent_at + timedelta(days=TOUCH_GAPS[touch_count - 1])


def open_sequence(prospect_id: str, *, audit_id: str | None = None,
                  now: datetime | None = None,
                  max_touches: int = MAX_TOUCHES) -> Sequence:
    """A sequence with nothing sent yet, due immediately. Touch one is day 0.

    `max_touches` comes from `touches_supported(len(pool))`: a prospect whose
    audit produced four findings gets two touches, and the sequence closes when
    it runs out of material rather than when the calendar does.
    """
    return Sequence(
        prospect_id=prospect_id,
        audit_id=audit_id,
        status=PENDING,
        touch_count=0,
        next_due_at=now or _utcnow(),
        max_touches=max(1, min(max_touches, MAX_TOUCHES)),
    )


def advance(seq: Sequence, *, sent_at: datetime | None = None) -> Sequence:
    """Record that a touch went out. Closes the sequence on the fourth."""
    if not seq.is_open:
        return seq
    sent_at = sent_at or _utcnow()
    count = seq.touch_count + 1
    following = due_after(count, sent_at, max_touches=seq.max_touches)
    spent = "sequence complete, no reply" if count >= MAX_TOUCHES else "no findings left to send"
    return replace(
        seq,
        status=ACTIVE if following is not None else CLOSED,
        touch_count=count,
        last_sent_at=sent_at,
        next_due_at=following,
        closed_reason=None if following is not None else spent,
    )


def apply_policy(seq: Sequence, intent: str, policy: ReplyPolicy, *,
                 at: datetime | None = None) -> Sequence:
    """Fold one reply into the sequence, given the policy for its intent.

    Split from `record_reply` so the state machine can be exercised against an
    explicit policy without depending on how `policy_for` is tuned.
    """
    at = at or _utcnow()
    count = seq.touch_count
    if not policy.burn_touch:
        # The touch did not land on a reader, so it should go again rather
        # than counting against the four. This is the only path that moves
        # the counter backward.
        count = max(0, count - 1)

    if policy.suppress or policy.close:
        return replace(
            seq, status=CLOSED, touch_count=count, next_due_at=None,
            last_intent=intent,
            closed_reason=("suppressed on reply" if policy.suppress else f"closed on {intent}"),
            revisit_at=(at + timedelta(days=policy.revisit_days)
                        if policy.revisit_days is not None and not policy.suppress else None),
        )

    if policy.needs_human:
        return replace(
            seq, status=WAITING, touch_count=count, next_due_at=None, last_intent=intent,
        )

    base = seq.last_sent_at or at
    following = due_after(count, base, max_touches=seq.max_touches) if count >= 1 else at
    if policy.defer_days is not None:
        following = at + timedelta(days=policy.defer_days)
    return replace(
        seq,
        status=ACTIVE if following is not None else CLOSED,
        touch_count=count,
        next_due_at=following,
        last_intent=intent,
        closed_reason=None if following is not None else "sequence complete",
    )


def record_reply(seq: Sequence, intent: str, *, at: datetime | None = None
                 ) -> tuple[Sequence, ReplyPolicy]:
    """Apply a reply using the configured policy.

    Returns the new sequence and the policy that produced it, because the
    caller has to perform the side effects the policy asks for: suppression is
    a Firestore write and this module does not do writes.
    """
    policy = policy_for(intent)
    return apply_policy(seq, intent, policy, at=at), policy


def park_reason(seq: Sequence) -> str:
    """Why this sequence is waiting, in the operator's words."""
    return PARK_REASONS.get(seq.last_intent or "", "a person to look")


def resume(seq: Sequence, *, now: datetime | None = None) -> Sequence:
    """Un-park a WAITING sequence once a human has dealt with whatever stopped it."""
    if seq.status != WAITING:
        return seq
    return replace(seq, status=ACTIVE, next_due_at=now or _utcnow())


# ── The policy table ──────────────────────────────────────────────────────────
#
# What each kind of reply does to the sequence. Every field defaults to the
# harmless value, so `ReplyPolicy()` means "keep going, change nothing", and a
# policy below says only what it actually changes.


POLICIES: dict[str, ReplyPolicy] = {
    # A live conversation. Stop the sequence, do not suppress: he is not a no,
    # he is a call to make, and suppressing him would block every later draft.
    INTERESTED: ReplyPolicy(close=True),

    # Criteria section 7: any request not to be contacted is permanent and
    # immediate. Suppression matches on every identifier we hold, so this ends
    # the prospect rather than the sequence.
    NOT_INTERESTED: ReplyPolicy(close=True, suppress=True),

    # "Not this quarter, ping me in Q3." Closing beats deferring. Resuming in
    # ninety days would send touch three of four against an audit a quarter
    # old, and the site may well have changed in between: a finding he has
    # already fixed is worse than no contact at all. Close it, and mark it to
    # be re-audited fresh, which starts a new sequence off current evidence.
    MAYBE_LATER: ReplyPolicy(close=True, revisit_days=90),

    # "Forwarding this to our head of growth." The touch never reached a
    # decision maker, so it should not count against the four. The forwarder
    # did us a favour and is not suppressed. The sequence parks until someone
    # supplies the name it was forwarded to.
    WRONG_PERSON: ReplyPolicy(burn_touch=False, needs_human=True),

    # An auto-responder read by nobody. It does not burn a touch, and it waits
    # a week rather than resending into the same empty desk tomorrow. A week
    # is a guess: most out of office messages state a return date and none of
    # them state it in a format worth parsing.
    OUT_OF_OFFICE: ReplyPolicy(burn_touch=False, defer_days=7),

    # Anything that fits none of the above. A person wrote back and we could
    # not tell what they meant, so a person reads it. Continuing the sequence
    # blind is the one outcome that is certainly wrong.
    OTHER: ReplyPolicy(burn_touch=False, needs_human=True),
}


def policy_for(intent: str) -> ReplyPolicy:
    """Map a reply intent to what it does to the sequence.

    An unrecognized intent is treated as OTHER rather than as nothing: a reply
    we cannot classify still came from a person, and the safe answer is to stop
    and let someone read it.
    """
    return POLICIES.get(intent, POLICIES[OTHER])
