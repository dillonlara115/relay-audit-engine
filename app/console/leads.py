"""The leads view: every prospect with a published report or any contact, and
where each one stands.

A lead's stage is derived from the ledger (touches, replies, the sequence)
until a person sets one by hand. The hand-set stages are what happens after a
reply, which no ledger can see: a call booked, a proposal out, won, lost.
Pure functions over plain rows, so the whole view is testable without a store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from app import outreach


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    pill: str      # the console's pill kinds: ok, warn, bad, dim, tint, info, running
    manual: bool = False


STAGES: tuple[Stage, ...] = (
    Stage("not_contacted", "Not contacted", "dim"),
    Stage("in_sequence", "In sequence", "tint"),
    Stage("replied", "Replied", "info"),
    Stage("call_booked", "Call booked", "running", manual=True),
    Stage("proposal_sent", "Proposal sent", "running", manual=True),
    Stage("won", "Won", "ok", manual=True),
    Stage("closed", "Closed", "bad"),
)
BY_KEY = {s.key: s for s in STAGES}

# What a person can choose. "lost" lands in the Closed column with its reason;
# "auto" hands the lead back to the ledger.
CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "Automatic"),
    ("call_booked", "Call booked"),
    ("proposal_sent", "Proposal sent"),
    ("won", "Won"),
    ("lost", "Lost"),
)
MANUAL_STAGES = frozenset(k for k, _ in CHOICES if k != "auto")
CHOICE_LABELS = dict(CHOICES)

TABS: tuple[tuple[str, str], ...] = (("action", "Needs action"), ("all", "All")) + tuple(
    (s.key, s.label) for s in STAGES)

_CHANNEL_WORD = {"email": "Email", "sms": "Text", "call": "Call"}
_CHANNEL_ICON = {"email": "mail", "sms": "message", "call": "phone"}


def _when(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _stamp(value: Any) -> str:
    when = _when(value)
    return when.strftime("%b %d") if when else ""


def home_audit(prospect_id: str, sequence: Mapping[str, Any] | None,
               published: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The audit a lead's row links to: the one its outreach runs on, else its
    most recently published report. A prospect audited in two sweeps has two
    audits; the lead has one home."""
    mine = [a for a in published if str(a.get("prospect_id")) == prospect_id]
    if sequence and sequence.get("audit_id"):
        for a in mine:
            if a.get("audit_id") == sequence["audit_id"]:
                return a
    if mine:
        return max(mine, key=lambda a: _when(a.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return None


def build(*, published: Sequence[Mapping[str, Any]], sequences: Iterable[Mapping[str, Any]],
          prospects: Mapping[str, Mapping[str, Any]],
          touches: Iterable[tuple[str, Mapping[str, Any]]],
          replies: Iterable[tuple[str, Mapping[str, Any]]],
          deals: Mapping[str, Mapping[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """One row per lead: a prospect with a published report or any contact."""
    now = now or datetime.now(timezone.utc)
    seq_by = {str(s.get("prospect_id")): s for s in sequences if s.get("prospect_id")}
    touches_by: dict[str, list[Mapping[str, Any]]] = {}
    for pid, t in touches:
        touches_by.setdefault(pid, []).append(t)
    replies_by: dict[str, list[Mapping[str, Any]]] = {}
    for pid, r in replies:
        replies_by.setdefault(pid, []).append(r)

    ids = {str(a.get("prospect_id")) for a in published if a.get("prospect_id")}
    ids |= {pid for pid, rows in touches_by.items() if rows}
    ids |= set(seq_by)
    rows = []
    for pid in sorted(ids):
        prospect = prospects.get(pid) or {}
        rows.append(_row(pid, prospect, seq_by.get(pid), home_audit(pid, seq_by.get(pid), published),
                         touches_by.get(pid, []), replies_by.get(pid, []), deals.get(pid) or {}, now))
    rows.sort(key=_order)
    return rows


def _row(pid: str, prospect: Mapping[str, Any], sequence: Mapping[str, Any] | None,
         audit: Mapping[str, Any] | None, touches: list[Mapping[str, Any]],
         replies: list[Mapping[str, Any]], deal: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    seq = outreach.Sequence.from_dict(sequence) if sequence else None
    outgoing = [t for t in touches if (t.get("direction") or "outgoing") != "incoming"]
    last_touch = max(outgoing, key=lambda t: _when(t.get("sent_at")) or now, default=None)
    last_reply = max(replies, key=lambda r: _when(r.get("received_at")) or now, default=None)
    intent = str((last_reply or {}).get("intent") or "")
    hand_set = str(deal.get("stage") or "")

    # ── stage ─────────────────────────────────────────────────────────────
    detail = ""
    if prospect.get("suppressed"):
        key, detail = "closed", "Suppressed"
    elif hand_set in MANUAL_STAGES:
        key = "closed" if hand_set == "lost" else hand_set
        detail = "Lost" if hand_set == "lost" else ""
    elif last_reply is not None:
        if intent == outreach.NOT_INTERESTED:
            key, detail = "closed", "Not interested"
        else:
            key = "replied"  # the reply's own pill says which kind
    elif outgoing:
        if seq and seq.status == outreach.CLOSED:
            key, detail = "closed", seq.closed_reason or "Sequence finished"
        else:
            key = "in_sequence"
    else:
        key = "not_contacted"
    stage = BY_KEY[key]

    # ── next step: the one thing to do, if anything ──────────────────────
    nxt, attention = "", False
    published = bool(audit and audit.get("report_slug"))
    if key in ("closed", "won"):
        nxt = ""
    elif key in ("call_booked", "proposal_sent"):
        nxt = "Follow up by hand"
    elif seq and seq.status == outreach.WAITING:
        nxt, attention = f"Needs you: {outreach.park_reason(seq)}", True
    elif key == "replied":
        nxt, attention = "Reply came in: set the stage once you talk", True
    elif seq and seq.is_open and seq.next_due_at is not None:
        n = seq.touch_count + 1
        if seq.due(now):
            nxt, attention = f"Email {n} of {seq.max_touches} due now", True
        else:
            nxt = f"Email {n} of {seq.max_touches} due {_stamp(seq.next_due_at)}"
    elif key == "not_contacted":
        nxt, attention = ("Send email 1", True) if published else ("Publish the report", True)

    channel = str((last_touch or {}).get("channel") or "email")
    last = None
    if last_touch is not None:
        words = _CHANNEL_WORD.get(channel, "Email")
        if channel == "email" and last_touch.get("ordinal"):
            words += f" {last_touch['ordinal']}"
        last = {"text": f"{words} · {_stamp(last_touch.get('sent_at'))}".strip(" ·"),
                "icon": _CHANNEL_ICON.get(channel, "mail"), "when": _when(last_touch.get("sent_at"))}

    reply = None
    if last_reply is not None:
        excerpt = " ".join(str(last_reply.get("excerpt") or "").split())
        reply = {"label": outreach.INTENT_LABELS.get(intent, "Reply"),
                 "kind": "ok" if intent == outreach.INTERESTED else "bad" if intent == outreach.NOT_INTERESTED else "info",
                 "excerpt": excerpt[:90] + ("..." if len(excerpt) > 90 else ""),
                 "when": _stamp(last_reply.get("received_at"))}

    scores = (audit or {}).get("scores") or {}
    return {
        "prospect_id": pid,
        "audit_id": str((audit or {}).get("audit_id") or prospect.get("latest_audit_id") or ""),
        "name": str(prospect.get("business_name") or "Unknown business"),
        "city": str(prospect.get("city") or ""),
        "phone": str(prospect.get("gbp_phone") or ""),
        "segment": (audit or {}).get("segment"),
        "total": scores.get("total"),
        "published": published,
        "stage": stage.key, "stage_label": stage.label, "stage_pill": stage.pill,
        "stage_detail": detail, "hand_set": hand_set if hand_set in MANUAL_STAGES else "auto",
        "next": nxt, "attention": attention, "last": last, "reply": reply,
        "emails_sent": seq.touch_count if seq else 0,
        "sort_last": (last or {}).get("when") or datetime.min.replace(tzinfo=timezone.utc),
    }


def _order(row: Mapping[str, Any]) -> tuple:
    """Needing action first, then furthest along the pipeline, then most
    recently touched."""
    position = [s.key for s in STAGES].index(row["stage"])
    return (not row["attention"], -position if row["stage"] != "closed" else 99,
            -row["sort_last"].timestamp())


def counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out = {"all": len(rows), "action": sum(1 for r in rows if r["attention"])}
    for s in STAGES:
        out[s.key] = sum(1 for r in rows if r["stage"] == s.key)
    return out


def filter_rows(rows: Sequence[Mapping[str, Any]], *, tab: str = "all", q: str = "") -> list[Mapping[str, Any]]:
    out = list(rows)
    if tab == "action":
        out = [r for r in out if r["attention"]]
    elif tab in BY_KEY:
        out = [r for r in out if r["stage"] == tab]
    needle = q.strip().lower()
    if needle:
        out = [r for r in out if needle in r["name"].lower() or needle in r["city"].lower()]
    return out


def columns(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The board: one column per stage, every stage shown even when empty, so
    the pipeline keeps its shape."""
    return [{"key": s.key, "label": s.label, "pill": s.pill,
             "rows": [r for r in rows if r["stage"] == s.key]} for s in STAGES]
