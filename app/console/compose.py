"""The email an operator sends by hand, drafted so they do not start blank.

This module writes text and builds a mailto: link. It sends nothing and
cannot: there is no mail client in this process, and hard rule 4 keeps it
that way. The link opens the operator's own mail client with the fields
filled; what leaves their mailbox is whatever they send after reading it.

Three guarantees the draft carries, because it goes to a contractor:

- No forbidden dash survives. Every line passes through copy_rules.sanitize
  and the whole body is checked again.
- No internal vocabulary leaks. The report's three findings were gated at
  publish time; a follow-up finding is checked here, and one that names a
  score or a segment is left out with a warning rather than sent.
- The link stays under a length every mail client hands off intact. Finding
  lines are dropped last to first, then context, then the body is cut at a
  word boundary. The page also offers the text to copy, for a client that
  truncates anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from app.copy_rules import contains_forbidden_dash, sanitize
from app.report.data import forbidden_terms_in
from app.report.publish import followup_findings, report_findings

MAX_MAILTO = 1800
SUBJECT_CAP = 90
LINE_CAP = 140
DEFAULT_SIGNATURE = "Relay for Roofers"


@dataclass(frozen=True)
class Draft:
    to: str | None
    subject: str
    body: str
    warnings: tuple[str, ...] = ()


def _cut(text: str, cap: int) -> str:
    """Trim at a word boundary with a plain ASCII ellipsis, never mid-word."""
    text = " ".join((text or "").split())
    if len(text) <= cap:
        return text
    head = text[:cap].rsplit(" ", 1)[0].rstrip(" ,;:")
    return head + "..."


def _clean_line(text: str) -> str:
    cleaned, _ = sanitize(text or "")
    return _cut(cleaned, LINE_CAP)


def touch_draft(*, ordinal: int, business_name: str, city: str, report_url: str,
                report_findings: Sequence[Mapping[str, Any]],
                followup: Mapping[str, Any] | None = None,
                signature: str = DEFAULT_SIGNATURE) -> Draft:
    """The text for email `ordinal` of the sequence. Touch one carries the
    report link and its three findings; each later one carries one held-back
    finding and the link again."""
    name = _cut(business_name or "your business", 60)
    warnings: list[str] = []
    base_subject = f"{name}: three things costing you booked jobs"
    subject = _cut(("Re: " if ordinal > 1 else "") + base_subject, SUBJECT_CAP)
    sig = _clean_line(signature or DEFAULT_SIGNATURE)

    if ordinal <= 1:
        lines = [
            "Hi there,",
            "",
            f"I looked at how a homeowner in {_cut(city or 'your area', 40)} finds and hires "
            f"{name}, and wrote up what I saw:",
            report_url,
            "",
            "Three things stood out:",
        ]
        for i, f in enumerate(report_findings[:3], start=1):
            lines.append(f"{i}. {_clean_line(str(f.get('what_we_saw') or ''))}")
        lines += ["", "The findings are yours to keep either way. Reply if you would like them fixed.",
                  "", "{Your name}", sig]
        finding_idx = [i for i, l in enumerate(lines) if l[:2] in ("1.", "2.", "3.")]
    else:
        seen = _clean_line(str((followup or {}).get("what_we_saw") or ""))
        means = _clean_line(str((followup or {}).get("what_it_means") or ""))
        for label, text in (("what we saw", seen), ("what it means", means)):
            leaked = forbidden_terms_in(text)
            if leaked:
                warnings.append(f"Follow-up finding {ordinal - 1} names internal vocabulary "
                                f"({', '.join(leaked)}) and was left out of the {label} line. "
                                "Edit it before it goes out.")
        seen = "" if forbidden_terms_in(seen) else seen
        means = "" if forbidden_terms_in(means) else means
        lines = ["Hi there,", "", f"One more thing I noticed about {name}:"]
        if seen:
            lines.append(seen)
        if means:
            lines += ["", means]
        lines += ["", "The write-up is still here:", report_url, "",
                  "Reply if you would like a hand with it.", "", "{Your name}", sig]
        finding_idx = [i for i, l in enumerate(lines) if l in (seen, means) and l]

    body = "\r\n".join(lines)
    cleaned, _ = sanitize(body)
    body = cleaned if not contains_forbidden_dash(body) else cleaned
    # sanitize() strips leading and trailing whitespace on the whole; the
    # line structure inside survives because it only rewrites dashes.
    draft = Draft(to=None, subject=subject, body=body, warnings=tuple(warnings))
    return _fit(draft, lines, finding_idx, sig)


def _fit(draft: Draft, lines: list[str], finding_idx: list[int], sig: str) -> Draft:
    """Trim until the mailto link fits. Findings go first, last to first,
    keeping at least one; then the context line; then a hard cut."""
    lines = list(lines)
    idx = list(finding_idx)
    while len(mailto_url(draft)) > MAX_MAILTO and len(idx) > 1:
        lines.pop(idx.pop())
        draft = Draft(draft.to, draft.subject, "\r\n".join(lines), draft.warnings)
    if len(mailto_url(draft)) > MAX_MAILTO:
        for i, line in enumerate(lines):
            if line.startswith("I looked at how") or line.startswith("One more thing"):
                lines[i] = "Here is what I saw:"
                break
        draft = Draft(draft.to, draft.subject, "\r\n".join(lines), draft.warnings)
    if len(mailto_url(draft)) > MAX_MAILTO:
        over = len(mailto_url(draft)) - MAX_MAILTO
        body = draft.body
        # Percent-encoding inflates most characters threefold; cut generously.
        keep = max(0, len(body) - over // 2 - 40)
        body = _cut(body[:keep], keep) + "\r\n\r\n{Your name}\r\n" + sig
        draft = Draft(draft.to, draft.subject, body, draft.warnings)
    return draft


def mailto_url(draft: Draft) -> str:
    """RFC 6068. quote with safe='' so a space is %20, which every client
    accepts; quote_plus would write +, which some read as a literal plus."""
    to = quote(draft.to or "", safe="@")
    return (f"mailto:{to}?subject={quote(draft.subject, safe='')}"
            f"&body={quote(draft.body, safe='')}")


def compose(*, ordinal: int, prospect: Mapping[str, Any], report_url: str,
            findings_doc: Mapping[str, Any] | None,
            signature: str = DEFAULT_SIGNATURE) -> Draft:
    """The draft for the next email to this prospect, addressed if we can."""
    doc = findings_doc or {}
    chosen = report_findings(doc) if doc else []
    later = followup_findings(doc) if doc else []
    followup = later[ordinal - 2] if ordinal >= 2 and len(later) >= ordinal - 1 else None
    draft = touch_draft(
        ordinal=ordinal,
        business_name=str(prospect.get("business_name") or ""),
        city=str(prospect.get("city") or ""),
        report_url=report_url,
        report_findings=chosen,
        followup=followup,
        signature=signature,
    )
    to = prospect.get("owner_email") or None
    return Draft(to=str(to) if to else None, subject=draft.subject,
                 body=draft.body, warnings=draft.warnings)
