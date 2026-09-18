"""The four emails as editable templates with variables.

An operator edits these in the console and reads the rendered result on the
prospect page before pressing Send. The defaults are the drafts the outreach
plan specified; they render to the same text the composer wrote before
templates existed, and a test holds them to that.

Rendering is substitution, not a template language. `{{business}}` becomes
the business name and nothing else can happen: no logic, no includes, no way
for a template to reach anything but the values listed in VARIABLES. A name
that is not on the list is left in place and reported, so a typo shows up on
screen instead of going out as braces.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from app.copy_rules import sanitize
from app.report.data import forbidden_terms_in
from app.report.publish import followup_findings, report_findings

MAX_TOUCHES = 4
SUBJECT_CAP = 120
BODY_CAP = 6000

# name -> what it becomes. The order is the order the Insert menu shows.
VARIABLES: dict[str, str] = {
    "first_name": "the owner's first name when one is on record, else \"there\"",
    "business": "the business name",
    "city": "the city on their Google profile",
    "domain": "their website's domain",
    "phone": "their phone number",
    "report_url": "the link to the published report",
    "findings": "the report's three findings as a numbered list",
    "finding_1": "the first report finding, one line",
    "finding_2": "the second report finding, one line",
    "finding_3": "the third report finding, one line",
    "followup": "the held-back finding for this email: what we saw",
    "followup_means": "the held-back finding for this email: what it means",
    "sender_name": "your name (OUTREACH_SENDER_NAME)",
    "signature": "the company line (OUTREACH_SIGNATURE)",
}

_FIRST_SUBJECT = "{{business}}: three things costing you booked jobs"

DEFAULTS: dict[int, dict[str, str]] = {
    1: {
        "subject": _FIRST_SUBJECT,
        "body": (
            "Hi {{first_name}},\n"
            "\n"
            "I looked at how a homeowner in {{city}} finds and hires {{business}}, and wrote up what I saw:\n"
            "{{report_url}}\n"
            "\n"
            "Three things stood out:\n"
            "{{findings}}\n"
            "\n"
            "The findings are yours to keep either way. Reply if you would like them fixed.\n"
            "\n"
            "{{sender_name}}\n"
            "{{signature}}"
        ),
    },
}
_FOLLOWUP_BODY = (
    "Hi {{first_name}},\n"
    "\n"
    "One more thing I noticed about {{business}}:\n"
    "{{followup}}\n"
    "\n"
    "{{followup_means}}\n"
    "\n"
    "The write-up is still here:\n"
    "{{report_url}}\n"
    "\n"
    "Reply if you would like a hand with it.\n"
    "\n"
    "{{sender_name}}\n"
    "{{signature}}"
)
for _n in range(2, MAX_TOUCHES + 1):
    DEFAULTS[_n] = {"subject": "Re: " + _FIRST_SUBJECT, "body": _FOLLOWUP_BODY}

# The one text. Short, signed, with the report link and the opt-out the
# carriers require. Sent by hand, one prospect at a time, like the emails.
TEXT_KEY = "sms"
TEXT_CAP = 320
DEFAULT_TEXT = ("Hi {{first_name}}, {{sender_name}} with Relay for Roofers here. I looked at how "
                "homeowners in {{city}} find {{business}} and wrote up three things costing you "
                "jobs: {{report_url}} Reply STOP to opt out.")

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render(template: str, values: Mapping[str, str]) -> tuple[str, list[str]]:
    """Substitute every known variable. Unknown names stay as written and are
    returned, in order of first appearance, without duplicates."""
    unknown: list[str] = []

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in VARIABLES:
            return str(values.get(name, ""))
        if name not in unknown:
            unknown.append(name)
        return m.group(0)

    return _VAR.sub(sub, template or ""), unknown


def _one_line(text: Any) -> str:
    cleaned, _ = sanitize(" ".join(str(text or "").split()))
    return cleaned


def _first_name(prospect: Mapping[str, Any]) -> str:
    name = " ".join(str(prospect.get("owner_name") or "").split())
    return name.split(" ", 1)[0] if name else "there"


def values_for(*, ordinal: int, prospect: Mapping[str, Any], report_url: str,
               findings_doc: Mapping[str, Any] | None, sender_name: str,
               signature: str) -> dict[str, str]:
    """Every variable, computed once for one email to one prospect. A value
    that is not known is an empty string, never a guess (hard rule 5)."""
    doc = findings_doc or {}
    chosen = report_findings(doc) if doc else []
    later = followup_findings(doc) if doc else []
    fu = later[ordinal - 2] if ordinal >= 2 and len(later) >= ordinal - 1 else {}
    lines = [_one_line(f.get("what_we_saw")) for f in chosen[:3]]
    return {
        "first_name": _first_name(prospect),
        "business": _one_line(prospect.get("business_name")) or "your business",
        "city": _one_line(prospect.get("city")) or "your area",
        "domain": _one_line(prospect.get("domain")),
        "phone": _one_line(prospect.get("gbp_phone") or prospect.get("phone")),
        "report_url": str(report_url or ""),
        "findings": "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1)),
        "finding_1": lines[0] if len(lines) > 0 else "",
        "finding_2": lines[1] if len(lines) > 1 else "",
        "finding_3": lines[2] if len(lines) > 2 else "",
        "followup": _one_line(fu.get("what_we_saw")),
        "followup_means": _one_line(fu.get("what_it_means")),
        "sender_name": _one_line(sender_name),
        "signature": _one_line(signature),
    }


def template_for(ordinal: int, saved: Mapping[str, Any] | None) -> dict[str, str]:
    """The stored template for this email, else the default. Stored keys are
    strings because Firestore map keys are."""
    row = (saved or {}).get(str(ordinal)) or (saved or {}).get(ordinal) or {}
    base = DEFAULTS.get(min(max(ordinal, 1), MAX_TOUCHES), DEFAULTS[MAX_TOUCHES])
    return {"subject": str(row.get("subject") or base["subject"]),
            "body": str(row.get("body") or base["body"])}


def text_template(saved: Mapping[str, Any] | None) -> str:
    row = (saved or {}).get(TEXT_KEY) or {}
    return str(row.get("body") or DEFAULT_TEXT)


def text_problems(body: str) -> list[str]:
    """Why a text template cannot be saved. The opt-out line is required."""
    out = [p for p in problems("text", body) if "subject" not in p.lower()]
    if len(body or "") > TEXT_CAP:
        out.append(f"A text is at most {TEXT_CAP} characters.")
    if "stop" not in (body or "").lower():
        out.append("A text must tell them how to opt out (for example: Reply STOP to opt out).")
    return out


def problems(subject: str, body: str) -> list[str]:
    """Why a template cannot be saved, in sentences. Empty means it can."""
    out: list[str] = []
    if not " ".join((subject or "").split()):
        out.append("The subject is empty.")
    if not (body or "").strip():
        out.append("The body is empty.")
    if len(subject or "") > SUBJECT_CAP:
        out.append(f"The subject is over {SUBJECT_CAP} characters.")
    if len(body or "") > BODY_CAP:
        out.append(f"The body is over {BODY_CAP} characters.")
    _, unknown = render((subject or "") + "\n" + (body or ""), {})
    if unknown:
        out.append("Unknown variable" + ("s" if len(unknown) > 1 else "") + ": "
                   + ", ".join("{{" + u + "}}" for u in unknown) + ".")
    leaked = forbidden_terms_in((subject or "") + " " + (body or ""))
    if leaked:
        out.append("Names internal vocabulary a contractor should never read: "
                   + ", ".join(leaked) + ".")
    return out


def clean(text: str) -> str:
    """Dashes fixed, line endings normalised, nothing else touched."""
    cleaned, _ = sanitize((text or "").replace("\r\n", "\n").replace("\r", "\n"))
    return cleaned


def normalise(form: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """Form fields subject_1..4 / body_1..4 into the stored shape."""
    out: dict[str, dict[str, str]] = {}
    for n in range(1, MAX_TOUCHES + 1):
        out[str(n)] = {"subject": clean(form.get(f"subject_{n}", "")).strip(),
                       "body": clean(form.get(f"body_{n}", "")).strip()}
    if f"body_{TEXT_KEY}" in form:
        out[TEXT_KEY] = {"body": " ".join(clean(form.get(f"body_{TEXT_KEY}", "")).split())}
    return out


def all_problems(templates: Mapping[str, Mapping[str, str]]) -> list[str]:
    out: list[str] = []
    for n in range(1, MAX_TOUCHES + 1):
        row = templates.get(str(n)) or {}
        for p in problems(row.get("subject", ""), row.get("body", "")):
            out.append(f"Email {n}: {p}")
    if TEXT_KEY in templates:
        for p in text_problems((templates.get(TEXT_KEY) or {}).get("body", "")):
            out.append(f"Text: {p}")
    return out


def sequence_of(templates: Mapping[str, Any] | None) -> Sequence[dict[str, str]]:
    return [template_for(n, templates) for n in range(1, MAX_TOUCHES + 1)]
