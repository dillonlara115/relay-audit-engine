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

INTENTS = (INTERESTED, MAYBE_LATER, WRONG_PERSON, NOT_INTERESTED, OUT_OF_OFFICE)

INTENT_LABELS = {
    INTERESTED: "Interested",
    MAYBE_LATER: "Maybe later",
    WRONG_PERSON: "Wrong person",
    NOT_INTERESTED: "Not interested",
    OUT_OF_OFFICE: "Out of office",
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
    needs_contact: bool = False  # park it until a human supplies a new address


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

    @property
    def touches_left(self) -> int:
        return max(0, MAX_TOUCHES - self.touch_count)

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
        for key in ("audit_id", "last_sent_at", "next_due_at", "last_intent", "closed_reason"):
            value = getattr(self, key)
            if value is not None:
                row[key] = value
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
        )


def due_after(touch_count: int, last_sent_at: datetime) -> datetime | None:
    """When the next touch comes due, or None when the sequence is spent."""
    if touch_count < 1 or touch_count >= MAX_TOUCHES:
        return None
    return last_sent_at + timedelta(days=TOUCH_GAPS[touch_count - 1])


def open_sequence(prospect_id: str, *, audit_id: str | None = None,
                  now: datetime | None = None) -> Sequence:
    """A sequence with nothing sent yet, due immediately. Touch one is day 0."""
    return Sequence(
        prospect_id=prospect_id,
        audit_id=audit_id,
        status=PENDING,
        touch_count=0,
        next_due_at=now or _utcnow(),
    )


def advance(seq: Sequence, *, sent_at: datetime | None = None) -> Sequence:
    """Record that a touch went out. Closes the sequence on the fourth."""
    if not seq.is_open:
        return seq
    sent_at = sent_at or _utcnow()
    count = seq.touch_count + 1
    following = due_after(count, sent_at)
    return replace(
        seq,
        status=ACTIVE if following is not None else CLOSED,
        touch_count=count,
        last_sent_at=sent_at,
        next_due_at=following,
        closed_reason=None if following is not None else "sequence complete, no reply",
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
        )

    if policy.needs_contact:
        return replace(
            seq, status=WAITING, touch_count=count, next_due_at=None, last_intent=intent,
        )

    base = seq.last_sent_at or at
    following = due_after(count, base) if count >= 1 else at
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


def resume(seq: Sequence, *, now: datetime | None = None) -> Sequence:
    """Un-park a WAITING sequence once a human has supplied a new contact."""
    if seq.status != WAITING:
        return seq
    return replace(seq, status=ACTIVE, next_due_at=now or _utcnow())


# ── The policy table ──────────────────────────────────────────────────────────
#
# TODO(dillon): this is the one decision in the ledger that is a judgement about
# how roofing owners actually reply, not a fact about the code. Fill in the five
# switches for each intent and delete the raise.
#
# `interested` and `not_interested` are obvious: close it, and close plus
# suppress. The three in the middle are the real call:
#
#   MAYBE_LATER    "Not this quarter, ping me in Q3." Section 6 says four
#                  touches and silence is a no, but this is not silence. Close
#                  it and re-audit later, or keep it alive with defer_days set
#                  to something long?
#   WRONG_PERSON   "Forwarding this to our head of growth." Does the original
#                  contact get suppressed, or just parked with needs_contact
#                  while the sequence waits for the new name? Suppressing the
#                  forwarder is safe but loses the thread.
#   OUT_OF_OFFICE  Almost certainly should not burn a touch, which is what
#                  burn_touch=False is for. Worth pairing with defer_days so it
#                  does not resend into the same empty desk tomorrow.
#
# Every field defaults to the harmless value, so a policy you have not thought
# about yet is `ReplyPolicy()`: keep going, change nothing.


def policy_for(intent: str) -> ReplyPolicy:
    """Map a reply intent to what it does to the sequence."""
    raise NotImplementedError(
        "policy_for is unset. See the TODO above: fill in the ReplyPolicy for "
        f"each of {', '.join(INTENTS)}."
    )
