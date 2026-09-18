"""Quo (formerly OpenPhone): contacts, one text at a time, and what comes back.

Quo is the phone system the operator already calls from. Three things this
module does, and one it does not:

- **Create a contact** for a prospect, keyed by the Google place id as
  Quo's externalId, so the call shows who is calling and so an inbound text
  or call can be matched back to the prospect. Idempotent: an existing
  contact with that externalId is reused, never duplicated.
- **Send one text** to one number, from the operator's Quo number, when a
  person has read it and pressed Send (hard rule 4). No list, no loop.
- **Verify and parse webhook deliveries**: an inbound text is a reply, an
  outbound call that connected is a touch, a call summary is notes. The
  signature is checked on the raw body before anything is read.
- It **never places a call**. Quo's API has no endpoint for that, and hard
  rule 3 would forbid it if it did. Calls happen in the Quo app, by hand;
  the tel: links in the console dial from there.

Auth is `Authorization: <api key>`, no Bearer. Ten requests a second per
key; this module makes at most a handful per click.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from app.config import get_config

BASE_URL = "https://api.quo.com/v1"
API_VERSION = "2026-03-30"
TIMEOUT = 20.0
TEXT_CAP = 320          # two SMS segments; a cold text longer than this is a letter
SOURCE = "relay-audit-engine"


class QuoUnavailable(RuntimeError):
    """Not configured, or Quo answered with something other than success."""


@dataclass(frozen=True)
class SentText:
    message_id: str
    conversation_id: str
    status: str


@dataclass(frozen=True)
class Event:
    """One webhook delivery, reduced to what the ledger needs."""
    kind: str                        # text_in | text_status | call_done | call_summary | other
    event_id: str
    resource_id: str                 # message id or call id
    direction: str = ""              # incoming | outgoing
    phone: str = ""                  # the other party, E.164 when Quo gives it
    text: str = ""
    status: str = ""
    duration: int = 0                # seconds, calls
    answered: bool = False
    contact_ids: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    link: str = ""
    raw_type: str = ""


def _headers() -> dict[str, str]:
    cfg = get_config()
    if not cfg.quo_api_key:
        raise QuoUnavailable("QUO_API_KEY is not set. Create a key under Quo workspace "
                             "settings, API, and put it in Secret Manager.")
    return {"Authorization": cfg.quo_api_key, "Quo-Api-Version": API_VERSION,
            "Content-Type": "application/json"}


def _request(method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None,
             client: httpx.Client | None = None) -> Any:
    headers = _headers()
    try:
        if client is not None:
            r = client.request(method, BASE_URL + path, headers=headers, json=json, params=params)
        else:
            with httpx.Client(timeout=TIMEOUT) as http:
                r = http.request(method, BASE_URL + path, headers=headers, json=json, params=params)
    except httpx.HTTPError as exc:
        raise QuoUnavailable(f"Quo did not answer: {exc}") from exc
    if r.status_code >= 400:
        raise QuoUnavailable(_explain(r))
    return r.json() if r.content else {}


def _explain(r: httpx.Response) -> str:
    """Quo's error codes, in the operator's words."""
    try:
        body = r.json()
    except ValueError:
        body = {}
    code = str(body.get("code") or (body.get("errors") or [{}])[0].get("code") or "")
    message = str(body.get("message") or body.get("title") or r.text[:200])
    known = {
        "0206400": "the Quo number is not approved for A2P 10DLC texting yet. Finish the "
                   "registration under Quo settings before texting businesses.",
        "0204403": "the Quo number has hit its 10DLC daily texting limit.",
        "0200401": "the Quo API key was refused. Check QUO_API_KEY.",
        "0201402": "the Quo subscription has expired.",
    }
    if code in known:
        return known[code]
    return f"Quo answered {r.status_code}: {message}"


# ── Contacts ──────────────────────────────────────────────────────────────────


def find_contact(external_id: str, *, client: httpx.Client | None = None) -> str | None:
    data = _request("GET", "/contacts", params={"externalIds": [external_id], "maxResults": 1},
                    client=client)
    rows = data.get("data") or []
    return str(rows[0]["id"]) if rows else None


def contact_payload(prospect: Mapping[str, Any], *, report_url: str = "",
                    console_url: str = "") -> dict[str, Any]:
    """What Quo shows when they call: the business as the name, the owner's
    name when known, the phone and email on record, and the report link as
    a custom note so the caller has it in front of them."""
    business = " ".join(str(prospect.get("business_name") or "").split()) or "Unknown roofer"
    owner = " ".join(str(prospect.get("owner_name") or "").split())
    first, last = (owner.split(" ", 1) + [""])[:2] if owner else (business, "")
    phones: list[dict[str, Any]] = []
    e164 = e164_of(prospect.get("gbp_phone") or prospect.get("phone"))
    if e164:
        phones.append({"name": "business", "value": e164})
    emails: list[dict[str, Any]] = []
    if prospect.get("owner_email"):
        emails.append({"name": "owner", "value": str(prospect["owner_email"])})
    custom: list[dict[str, Any]] = []
    if report_url:
        custom.append({"name": "Relay report", "value": report_url})
    if console_url:
        custom.append({"name": "Relay prospect page", "value": console_url})
    if prospect.get("city"):
        custom.append({"name": "City", "value": str(prospect["city"])})
    payload: dict[str, Any] = {
        "defaultFields": {"firstName": first, "lastName": last or None,
                          "company": business, "role": "Owner" if owner else None,
                          "phoneNumbers": phones, "emails": emails},
        "source": SOURCE,
        "externalId": str(prospect.get("place_id") or prospect.get("prospect_id") or "")[:75] or None,
    }
    if custom:
        payload["customFields"] = custom
    return payload


def ensure_contact(prospect: Mapping[str, Any], *, report_url: str = "", console_url: str = "",
                   client: httpx.Client | None = None) -> str:
    """The Quo contact id for this prospect, creating it once."""
    payload = contact_payload(prospect, report_url=report_url, console_url=console_url)
    external = payload.get("externalId")
    if external:
        found = find_contact(external, client=client)
        if found:
            return found
    data = _request("POST", "/contacts", json=payload, client=client)
    row = data.get("data") or data
    return str(row.get("id") or "")


# ── Texts ─────────────────────────────────────────────────────────────────────


def e164_of(raw: Any) -> str:
    from app.tools.phones import parse_phone

    parsed = parse_phone(str(raw or ""))
    return parsed.e164 if parsed else ""


def send_text(*, to: str, content: str, client: httpx.Client | None = None) -> SentText:
    """Send one text to one number, now, from the configured Quo number.

    Called from exactly one place, the console route behind the Send text
    button, after suppression, the number, the copy checks and the daily cap
    have all passed. Takes no list.
    """
    cfg = get_config()
    if not cfg.quo_from:
        raise QuoUnavailable("QUO_FROM is not set: the Quo number texts go out from, in "
                             "+1 form.")
    number = e164_of(to)
    if not number:
        raise ValueError(f"send_text needs one phone number, got {to!r}")
    text = " ".join(content.split())
    if not text:
        raise ValueError("send_text needs some text")
    if len(text) > TEXT_CAP:
        raise ValueError(f"a text is at most {TEXT_CAP} characters, this one is {len(text)}")
    data = _request("POST", "/messages", json={"content": text, "from": cfg.quo_from,
                                               "to": [number]}, client=client)
    row = data.get("data") or data
    return SentText(message_id=str(row.get("id") or ""),
                    conversation_id=str(row.get("conversationId") or ""),
                    status=str(row.get("status") or "queued"))


# ── Setup ─────────────────────────────────────────────────────────────────────


def phone_numbers(*, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """The workspace's numbers: id (PN...), number (+1...), name, users."""
    data = _request("GET", "/phone-numbers", client=client)
    return list(data.get("data") or [])


WEBHOOK_EVENTS = ("message.received", "message.delivered", "message.failed", "message.undelivered",
                  "call.completed", "call.summary.completed")


def create_webhook(url: str, *, label: str = "relay-audit-engine",
                   client: httpx.Client | None = None) -> dict[str, Any]:
    """Register our endpoint for the events the ledger reads. The response
    carries the signing key once; the caller stores it as QUO_WEBHOOK_KEY."""
    data = _request("POST", "/webhooks", json={"url": url, "events": list(WEBHOOK_EVENTS),
                                               "resourceIds": ["*"], "label": label,
                                               "status": "enabled"}, client=client)
    return data.get("data") or data


# ── Webhooks ──────────────────────────────────────────────────────────────────


def verify_signature(headers: Mapping[str, str], raw_body: bytes, key: str) -> bool:
    """Quo signs `{webhook-id}.{webhook-timestamp}.{raw body}` with HMAC-SHA256
    under the base64 key after its whsec_ prefix, and sends one or more
    `v1,<base64>` entries space-separated in webhook-signature. Compared in
    constant time; the raw bytes must be the ones Quo sent."""
    if not key:
        return False
    lower = {k.lower(): v for k, v in headers.items()}
    wid, ts, sig = lower.get("webhook-id", ""), lower.get("webhook-timestamp", ""), lower.get("webhook-signature", "")
    if not (wid and ts and sig):
        return False
    secret = key[len("whsec_"):] if key.startswith("whsec_") else key
    try:
        secret_bytes = base64.b64decode(secret)
    except ValueError:
        return False
    signed = f"{wid}.{ts}.".encode() + raw_body
    expected = base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
    for entry in sig.split():
        version, _, value = entry.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def sign(raw_body: bytes, key: str, *, webhook_id: str, timestamp: str) -> dict[str, str]:
    """The headers Quo would send for this body: for tests and for the CLI's
    send-test-event. The inverse of verify_signature."""
    secret = key[len("whsec_"):] if key.startswith("whsec_") else key
    digest = hmac.new(base64.b64decode(secret), f"{webhook_id}.{timestamp}.".encode() + raw_body,
                      hashlib.sha256).digest()
    return {"webhook-id": webhook_id, "webhook-timestamp": timestamp,
            "webhook-signature": "v1," + base64.b64encode(digest).decode()}


_E164 = re.compile(r"^\+\d{8,15}$")


def _other_party(data: Mapping[str, Any], direction: str) -> str:
    ctx = data.get("context") or {}
    if direction == "incoming":
        candidates = [ctx.get("senderIdentifier")]
    else:
        candidates = list(ctx.get("recipientIdentifiers") or [])
    for p in ctx.get("participants") or []:
        candidates.append(p if isinstance(p, str) else (p or {}).get("phoneNumber"))
    for c in candidates:
        if isinstance(c, str) and _E164.match(c):
            return c
    return ""


def parse_event(payload: Mapping[str, Any]) -> Event:
    """Reduce a delivery to what the ledger needs. Unknown types come back as
    kind "other" so the route can acknowledge them without acting."""
    kind_map = {"message.received": "text_in", "message.delivered": "text_status",
                "message.failed": "text_status", "message.undelivered": "text_status",
                "call.completed": "call_done", "call.summary.completed": "call_summary"}
    raw_type = str(payload.get("type") or "")
    data = payload.get("data") or {}
    res = data.get("resource") or {}
    ctx = data.get("context") or {}
    direction = str(res.get("direction") or "")
    kind = kind_map.get(raw_type, "other")
    contact_ids = tuple(str(c) for c in ((ctx.get("contacts") or {}).get("ids") or []))
    link = str(((data.get("links") or {}).get("quo")) or "")
    if kind == "call_summary":
        return Event(kind=kind, event_id=str(payload.get("id") or ""),
                     resource_id=str(res.get("callId") or ""), contact_ids=contact_ids,
                     summary=tuple(str(s) for s in res.get("summary") or []),
                     next_steps=tuple(str(s) for s in res.get("nextSteps") or []),
                     link=link, raw_type=raw_type, phone=_other_party(data, "outgoing"))
    duration = int(res.get("duration") or 0)
    return Event(kind=kind, event_id=str(payload.get("id") or ""),
                 resource_id=str(res.get("id") or ""), direction=direction,
                 phone=_other_party(data, direction),
                 text=" ".join(str(res.get("text") or "").split()),
                 status=str(res.get("status") or ""), duration=duration,
                 answered=bool(res.get("answeredAt")) or duration > 0,
                 contact_ids=contact_ids, link=link, raw_type=raw_type)


STOP_WORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})


def is_stop(text: str) -> bool:
    """The carrier opt-out keywords. Quo honours them itself; the ledger has to
    as well, so a suppression lands before anyone can send again."""
    return " ".join((text or "").lower().split()).strip(".!") in STOP_WORDS
