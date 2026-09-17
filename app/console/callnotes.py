"""Call notes for one prospect: what to say when a person picks up the phone.

Built from what is already on record, nothing invented (hard rule 5): the
report's three findings, the held-back ones, the checks that passed with a
note worth a compliment, where the outreach stands, and a fixed set of
lines per segment about how to pitch. No model call; the findings are
already written in plain language, and a script that changes between page
loads is worse than one the caller has read twice.

Everything here is for the operator's eyes. The lines under "Say" are in
outcome language, so they can be read aloud; scores, bands and segment
names stay in the framing for the caller, never in a line meant for the
owner.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.copy_rules import sanitize
from app.report.publish import followup_findings, report_findings

ANGLES: dict[str, list[str]] = {
    "Leaky Bucket": [
        "Customers already find them. The problem is what happens after: leads that slip away.",
        "Lead with the leak, not the site. The fix is small and quick, which is the pitch.",
    ],
    "Invisible Pro": [
        "The site and the intake are fine; nobody arrives. Sell visibility, not a rebuild.",
        "Ask how many calls a week come from Google. The gap between that and their reviews is the story.",
    ],
    "Both Broken": [
        "Weak on both sides. A bigger project and a slower close; do not oversell the quick fix.",
        "Pick the one finding that costs them a job this month and stay on it.",
    ],
    "Dialed": [
        "Doing well already. Not a prospect for the audit; a referral conversation.",
        "Ask who else they know who is struggling with leads, and offer the write-up as a favour.",
    ],
    "incomplete": [
        "We could not finish checking the site, so the scores understate. Say only what the findings say.",
        "Offer a fresh look rather than a verdict.",
    ],
}

RULES = [
    "Never quote a score, a band or a segment name. Never say how anything was measured.",
    "Every claim on this page traces to something we saw. If they dispute one, say you will check and move on.",
    "If they say stop, thank them, end the call, and suppress the prospect on this page.",
]


def _line(text: Any) -> str:
    cleaned, _ = sanitize(" ".join(str(text or "").split()))
    return cleaned


def build(*, prospect: Mapping[str, Any], audit: Mapping[str, Any],
          checks: Sequence[Mapping[str, Any]], definitions: Mapping[str, Any],
          findings_doc: Mapping[str, Any] | None, touches: Sequence[Mapping[str, Any]] = (),
          replies: Sequence[Mapping[str, Any]] = (), report_url: str | None = None,
          intent_labels: Mapping[str, str] | None = None) -> dict[str, Any]:
    segment = str(audit.get("segment") or "incomplete")
    doc = findings_doc or {}
    chosen = report_findings(doc) if doc else []
    later = followup_findings(doc) if doc else []
    by_code = {str(c.get("code")): c for c in checks}

    def ordered(section: str, status: str) -> list[Mapping[str, Any]]:
        rows = []
        for code, d in definitions.items():
            c = by_code.get(str(code))
            if c and d.get("section") == section and c.get("status") == status and _line(c.get("note")):
                rows.append((d.get("sort_order", 0), c, d))
        rows.sort(key=lambda r: r[0])
        return [dict(c, title=d.get("title", "")) for _, c, d in rows]

    strengths = (ordered("found", "pass") + ordered("chosen", "pass"))[:2]
    weak = (ordered("booked", "fail") + ordered("found", "fail") + ordered("chosen", "fail"))[:3]

    raise_lines = [{"saw": _line(f.get("what_we_saw")), "means": _line(f.get("what_it_means"))}
                   for f in chosen[:3]]
    source = "report"
    if not raise_lines:
        source = "checks"
        raise_lines = [{"saw": _line(c.get("note")), "means": ""} for c in weak]
    more = [_line(f.get("what_we_saw")) for f in later if _line(f.get("what_we_saw"))]

    sent = sorted((t for t in touches), key=lambda t: t.get("ordinal") or 0)
    last_reply = None
    for r in replies:
        if last_reply is None or (r.get("received_at") or 0) > (last_reply.get("received_at") or 0):
            last_reply = r
    labels = intent_labels or {}
    if not sent and not last_reply:
        standing = "No email has gone out yet. This would be the first contact."
    else:
        standing = f"{len(sent)} email{'s' if len(sent) != 1 else ''} sent"
        if sent and hasattr(sent[-1].get("sent_at"), "strftime"):
            standing += f", the last on {sent[-1]['sent_at'].strftime('%b %d')}"
        standing += "."
        if last_reply:
            label = labels.get(str(last_reply.get("intent") or ""), str(last_reply.get("intent") or "reply"))
            standing += f" They replied: {label}."
            if _line(last_reply.get("excerpt")):
                standing += f' "{_line(last_reply.get("excerpt"))[:120]}"'
        else:
            standing += " No reply yet; this call is the follow-up."

    ask = []
    if report_url:
        ask.append(f"Offer to send the write-up: {report_url}")
    else:
        ask.append("Offer to send the write-up once the report is published.")
    ask.append("Ask who looks after the website and when they last saw it on a phone.")
    ask.append("Close on one of two things: a fifteen-minute walk-through this week, or the "
               "write-up by email and a call back on a day they name.")

    who = {
        "business": _line(prospect.get("business_name")),
        "city": _line(prospect.get("city")),
        "phone": _line(prospect.get("gbp_phone") or prospect.get("phone")),
        "domain": _line(prospect.get("domain")),
        "owner": _line(prospect.get("owner_name")),
        "email": _line(prospect.get("owner_email")),
        "segment": "Incomplete" if segment == "incomplete" else segment,
    }
    return {
        "who": who,
        "angle": ANGLES.get(segment, ANGLES["incomplete"]),
        "strengths": [{"title": _line(c.get("title")), "note": _line(c.get("note"))} for c in strengths],
        "raise": raise_lines,
        "raise_source": source,
        "more": more,
        "ask": ask,
        "standing": standing,
        "rules": RULES,
        "text": "",
    } | {"text": _as_text(who, ANGLES.get(segment, ANGLES["incomplete"]), strengths,
                          raise_lines, source, more, ask, standing)}


def _as_text(who, angle, strengths, raise_lines, source, more, ask, standing) -> str:
    out = [f"CALL NOTES: {who['business']}" + (f", {who['city']}" if who["city"] else "")]
    bits = [b for b in (who["phone"], who["domain"], who["owner"], who["email"]) if b]
    if bits:
        out.append("  " + " | ".join(bits))
    out += ["", "WHERE THEY STAND", f"  {standing}", "", "THE ANGLE"]
    out += [f"  - {a}" for a in angle]
    if strengths:
        out += ["", "OPEN WITH (what is working)"]
        out += [f"  - {s.get('title')}: {_line(s.get('note'))}" for s in strengths]
    out += ["", "RAISE" + (" (from the report)" if source == "report" else " (from the checks; no findings drafted yet)")]
    for i, r in enumerate(raise_lines, start=1):
        out.append(f"  {i}. {r['saw']}")
        if r["means"]:
            out.append(f"     {r['means']}")
    if more:
        out += ["", "IF THEY WANT MORE"]
        out += [f"  - {m}" for m in more]
    out += ["", "THE ASK"] + [f"  - {a}" for a in ask]
    out += ["", "RULES"] + [f"  - {r}" for r in RULES]
    return "\n".join(out)
