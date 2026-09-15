"""Pulling replies in and letting the ledger act on them.

This is the loop criteria section 7 has been waiting for. Until a reply is
recorded, "thirty hand sent and a reply rate is known" is not a number anybody
can produce, and the sequence has no way to know it has been answered.

The order matters and is deliberate:

    find open sequences -> collect their known addresses -> search only those
    -> classify -> apply the policy -> write the suppression the policy asked
    for -> save the sequence

Classification and consequence are kept apart. `app/agents/classifier.py`
returns an intent and nothing else; `app/outreach.POLICIES` decides what that
intent does. A model never suppresses anybody, it only says what it thinks a
person meant, and an operator can correct the label afterwards without the
damage already being done.

Nothing here sends. The Gmail credential carries `gmail.readonly` and the
module that builds it refuses anything wider.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from app import outreach
from app.agents.classifier import Classification, classify
from app.store import firestore as store
from app.tools.gmail import GmailUnavailable, Reply, fetch_replies

# How far back to look when a sequence has somehow lost its last_sent_at. A
# reply older than the touch that prompted it is not a reply to it.
DEFAULT_LOOKBACK_DAYS = 30


@dataclass
class ReplyOutcome:
    prospect_id: str
    business_name: str
    reply: Reply
    classification: Classification
    intent: str
    closed: bool = False
    suppressed: bool = False
    parked: bool = False


@dataclass
class ScanResult:
    scanned: int = 0
    found: int = 0
    skipped_seen: int = 0
    outcomes: list[ReplyOutcome] = field(default_factory=list)
    error: str | None = None


def addresses_for(prospect: Mapping[str, Any]) -> list[str]:
    """Every address we hold for this prospect, best first.

    Manual first because a person put it there. Only deliverable discovered
    addresses follow: searching on one we already know is dead would widen the
    query for nothing.
    """
    out: list[str] = []
    for row in prospect.get("manual_contacts") or []:
        if row.get("email"):
            out.append(str(row["email"]).lower())
    for row in prospect.get("contacts") or []:
        if row.get("email") and row.get("status") in ("valid", "risky", "unknown"):
            out.append(str(row["email"]).lower())
    return list(dict.fromkeys(out))


def _suppress_for(prospect_id: str, prospect: Mapping[str, Any], reply: Reply) -> None:
    """Permanent and immediate, per criteria section 7.

    Suppressed on the address that actually replied as well as on the prospect,
    so a later sweep that rediscovers the business cannot reopen it and a
    different prospect sharing that address is caught too.
    """
    store.add_suppression("email", reply.from_email, "replied: not interested")
    store.add_suppression("place_id", prospect_id, "replied: not interested")
    domain = prospect.get("domain")
    if domain:
        store.add_suppression("domain", str(domain), "replied: not interested")
    store.mark_suppressed(prospect_id, "replied: not interested")


async def _handle(prospect_id: str, sequence_row: Mapping[str, Any],
                  reply: Reply, *, prospect: Mapping[str, Any]) -> ReplyOutcome:
    classification = await classify(reply.excerpt, subject=reply.subject)
    intent = classification.effective_intent

    seq = outreach.Sequence.from_dict(sequence_row)
    policy = outreach.policy_for(intent)
    advanced = outreach.apply_policy(seq, intent, policy, at=reply.received_at)

    store.add_reply(prospect_id, {
        **reply.to_dict(),
        "intent": intent,
        "classification": classification.to_dict(),
        "touch_ordinal": seq.touch_count,
    })

    if policy.suppress:
        _suppress_for(prospect_id, prospect, reply)

    store.save_sequence(advanced)

    return ReplyOutcome(
        prospect_id=prospect_id,
        business_name=str(prospect.get("business_name") or prospect_id),
        reply=reply,
        classification=classification,
        intent=intent,
        closed=advanced.status == outreach.CLOSED,
        suppressed=policy.suppress,
        parked=advanced.status == outreach.WAITING,
    )


async def scan(*, limit: int = 50, service: Any = None,
               now: datetime | None = None) -> ScanResult:
    """Read replies for every sequence that is waiting on one.

    A sequence with nothing sent cannot have been replied to, so `pending` is
    not scanned. `waiting` is, because a parked sequence can still receive a
    second message.
    """
    now = now or datetime.now(timezone.utc)
    result = ScanResult()

    rows = [r for status in (outreach.ACTIVE, outreach.WAITING)
            for r in store.sequences_by_status(status, limit=limit)]
    result.scanned = len(rows)
    if not rows:
        return result

    for row in rows:
        prospect_id = str(row.get("prospect_id") or "")
        if not prospect_id:
            continue
        prospect = store.get_prospect(prospect_id) or {}
        addresses = addresses_for(prospect)
        if not addresses:
            continue

        since = row.get("last_sent_at") or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))

        try:
            replies = await asyncio.to_thread(
                fetch_replies, addresses, since=since, service=service
            )
        except GmailUnavailable as exc:
            result.error = str(exc)
            return result

        seen = {r.get("message_id") for r in store.replies_for(prospect_id)}
        fresh = [r for r in replies if r.message_id not in seen]
        result.skipped_seen += len(replies) - len(fresh)
        if not fresh:
            continue

        # Oldest first: two replies in one scan should land on the sequence in
        # the order they were written, not the order Gmail returned them.
        for reply in sorted(fresh, key=lambda r: r.received_at):
            current = store.get_sequence(prospect_id) or row
            outcome = await _handle(prospect_id, current, reply, prospect=prospect)
            result.outcomes.append(outcome)
            result.found += 1

    return result


def reply_rate(days: int = 365) -> dict[str, Any]:
    """The section 7 number, and what it is made of.

    Counts touches sent rather than prospects contacted, because the threshold
    is written about messages. A prospect who replied to touch two contributes
    two sends and one reply.
    """
    sent = 0
    replied = 0
    by_intent: dict[str, int] = {}
    by_segment: dict[str, dict[str, int]] = {}

    for row in store.all_sequences():
        prospect_id = row.get("prospect_id")
        if not prospect_id:
            continue
        touches = store.touches_for(prospect_id)
        replies = store.replies_for(prospect_id)
        sent += len(touches)
        replied += len(replies)
        for reply in replies:
            intent = str(reply.get("intent") or outreach.OTHER)
            by_intent[intent] = by_intent.get(intent, 0) + 1
        # Segment lives on the audit, not denormalized onto the sequence. One
        # extra read per prospect, and the ledger holds a hundred a month.
        audit = store.get_audit(str(row.get("audit_id"))) if row.get("audit_id") else None
        segment = str((audit or {}).get("segment") or "unsegmented")
        bucket = by_segment.setdefault(segment, {"sent": 0, "replied": 0})
        bucket["sent"] += len(touches)
        bucket["replied"] += len(replies)

    return {
        "sent": sent,
        "replied": replied,
        # Absent rather than zero when nothing has gone out: an unmeasured rate
        # is not a rate of nought.
        "rate": (replied / sent) if sent else None,
        "by_intent": by_intent,
        "by_segment": by_segment,
        "threshold": 30,
        "meets_threshold": sent >= 30,
    }
