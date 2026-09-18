"""What a Quo webhook delivery does to the ledger.

An inbound text is a reply: classified like an email reply, recorded, and
the sequence parked or closed by the same policy. A STOP is a suppression
before anything else. A completed call is a touch (outgoing) or a reply
(incoming), and a call summary is attached to the call it belongs to. A
delivery about a number nobody on the call list has is acknowledged and
ignored; this system holds no record of anyone it did not audit.

Nothing here sends. Hard rule 3 stands: calls are placed by hand in Quo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from app import outreach
from app.agents.classifier import classify
from app.store import firestore as store
from app.tools import quo


@dataclass(frozen=True)
class Outcome:
    action: str                      # ignored | duplicate | suppressed | reply | touch | summary | unknown
    prospect_id: str = ""
    intent: str = ""
    detail: str = ""


def _prospect_for(event: quo.Event) -> dict[str, Any] | None:
    for cid in event.contact_ids:
        found = store.prospect_by_quo_contact(cid)
        if found:
            return found
    if event.phone:
        return store.prospect_by_phone(event.phone)
    return None


def _sequence(prospect_id: str) -> outreach.Sequence:
    row = store.get_sequence(prospect_id)
    return outreach.Sequence.from_dict(row) if row else outreach.open_sequence(prospect_id)


def _suppress(prospect_id: str, prospect: Mapping[str, Any], phone: str, reason: str) -> None:
    if phone:
        store.add_suppression("phone", phone, reason)
    store.add_suppression("place_id", prospect_id, reason)
    if prospect.get("domain"):
        store.add_suppression("domain", str(prospect["domain"]), reason)
    store.mark_suppressed(prospect_id, reason)


async def handle(payload: Mapping[str, Any]) -> Outcome:
    event = quo.parse_event(payload)
    if event.kind in ("other", "text_status"):
        return Outcome("ignored", detail=event.raw_type)
    if not store.claim_event(event.event_id):
        return Outcome("duplicate", detail=event.event_id)

    prospect = _prospect_for(event)
    if not prospect:
        return Outcome("unknown", detail=event.phone or ",".join(event.contact_ids))
    pid = str(prospect.get("place_id") or "")
    now = datetime.now(timezone.utc)

    if event.kind == "text_in":
        if quo.is_stop(event.text):
            _suppress(pid, prospect, event.phone, "texted STOP")
            seq = _sequence(pid)
            closed = outreach.apply_policy(seq, outreach.NOT_INTERESTED,
                                           outreach.policy_for(outreach.NOT_INTERESTED), at=now)
            store.save_sequence(closed)
            store.add_reply(pid, {"channel": "sms", "from_phone": event.phone, "excerpt": event.text,
                                  "received_at": now, "intent": outreach.NOT_INTERESTED,
                                  "message_id": event.resource_id, "link": event.link})
            return Outcome("suppressed", pid, outreach.NOT_INTERESTED, "texted STOP")
        classification = await classify(event.text)
        intent = classification.effective_intent
        seq = _sequence(pid)
        policy = outreach.policy_for(intent)
        advanced = outreach.apply_policy(seq, intent, policy, at=now)
        store.add_reply(pid, {"channel": "sms", "from_phone": event.phone, "excerpt": event.text[:500],
                              "received_at": now, "intent": intent,
                              "classification": classification.to_dict(),
                              "touch_ordinal": seq.touch_count, "message_id": event.resource_id,
                              "link": event.link})
        if policy.suppress:
            _suppress(pid, prospect, event.phone, f"replied by text: {intent}")
        store.save_sequence(advanced)
        return Outcome("reply", pid, intent, event.text[:80])

    if event.kind == "call_done":
        store.add_touch(pid, {
            "channel": "call", "direction": event.direction or "outgoing", "sent_at": now,
            "duration": event.duration, "answered": event.answered, "to": event.phone,
            "resource_id": event.resource_id, "link": event.link, "logged_via": "quo",
        })
        if not store.get_sequence(pid):
            store.save_sequence(outreach.open_sequence(pid))
        return Outcome("touch", pid, detail=f"{event.direction} call, {event.duration}s")

    if event.kind == "call_summary":
        found = store.touch_by_resource(pid, event.resource_id)
        if not found:
            return Outcome("ignored", pid, detail="summary for a call not on record")
        touch_id, _ = found
        store.update_touch(pid, touch_id, {"summary": list(event.summary),
                                           "next_steps": list(event.next_steps)})
        return Outcome("summary", pid, detail="; ".join(event.summary)[:120])

    return Outcome("ignored", pid, detail=event.raw_type)
