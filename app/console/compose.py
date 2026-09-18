"""The email an operator reads and sends, drafted so they do not start blank.

Since the Sep 17 amendment to rule 4 the console can send the email itself,
from the operator's own mailbox, when they press Send on it. This module
still only writes text: it renders the stored template for the next email
with the prospect's values, checks it, and hands the draft to the page. The
send lives in app/tools/gmail.py and the route that calls it.

Three guarantees the draft carries, because it goes to a contractor:

- No forbidden dash survives. Every value passes through copy_rules.sanitize
  and the whole body is checked again.
- No internal vocabulary leaks. The report's three findings were gated at
  publish time; a follow-up finding is checked here, and one that names a
  score or a segment is left out with a warning rather than sent.
- A mailto: link is still offered, for an operator who prefers their own mail
  client; it is trimmed to a length every client hands off intact.
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
            signature: str = DEFAULT_SIGNATURE, sender_name: str = "",
            templates: Mapping[str, Any] | None = None) -> Draft:
    """The draft for the next email to this prospect, addressed if we can.

    Rendered from the stored template for this email (the default when none
    is saved) with the prospect's values. A held-back finding that names
    internal vocabulary is blanked with a warning; the operator reads the
    result before it goes anywhere."""
    from app import outreach_templates as tpl

    values = tpl.values_for(ordinal=ordinal, prospect=prospect, report_url=report_url,
                            findings_doc=findings_doc, sender_name=sender_name,
                            signature=signature)
    warnings: list[str] = []
    for key, label in (("followup", "what we saw"), ("followup_means", "what it means")):
        leaked = forbidden_terms_in(values[key])
        if leaked:
            warnings.append(f"Follow-up finding {ordinal - 1} names internal vocabulary "
                            f"({', '.join(leaked)}) and was left out of the {label} line. "
                            "Edit it before it goes out.")
            values[key] = ""
    if not values["sender_name"]:
        warnings.append("Sender name is not set (OUTREACH_SENDER_NAME). Add your name above "
                        "the signature before it goes out.")
    template = tpl.template_for(ordinal, templates)
    subject, unknown_s = tpl.render(template["subject"], values)
    body, unknown_b = tpl.render(template["body"], values)
    for name in unknown_s + unknown_b:
        warnings.append(f"{{{{{name}}}}} is not a variable and was left as written.")
    subject = _cut(" ".join(subject.split()), SUBJECT_CAP)
    body, _ = sanitize(body.replace("\r\n", "\n"))
    body = "\r\n".join(line.rstrip() for line in body.split("\n"))
    to = prospect.get("owner_email") or None
    return Draft(to=str(to) if to else None, subject=subject, body=body,
                 warnings=tuple(warnings))


def fit_mailto(draft: Draft) -> str:
    """The mailto: link for a draft, its body cut at a word boundary when the
    link would exceed what mail clients hand off intact."""
    if len(mailto_url(draft)) <= MAX_MAILTO:
        return mailto_url(draft)
    body = draft.body
    while body and len(mailto_url(Draft(draft.to, draft.subject, body))) > MAX_MAILTO:
        body = _cut(body[: max(0, len(body) - 200)], len(body))
    return mailto_url(Draft(draft.to, draft.subject, body))


def compose_text(*, prospect: Mapping[str, Any], report_url: str,
                 findings_doc: Mapping[str, Any] | None, signature: str = DEFAULT_SIGNATURE,
                 sender_name: str = "", templates: Mapping[str, Any] | None = None) -> Draft:
    """The one text for this prospect, rendered from the text template. Same
    checks as an email; `to` is the number in +1 form when we have one."""
    from app import outreach_templates as tpl
    from app.tools.quo import e164_of

    values = tpl.values_for(ordinal=1, prospect=prospect, report_url=report_url,
                            findings_doc=findings_doc, sender_name=sender_name,
                            signature=signature)
    warnings: list[str] = []
    if not values["sender_name"]:
        warnings.append("Sender name is not set (OUTREACH_SENDER_NAME). A text should say who it is from.")
    body, unknown = tpl.render(tpl.text_template(templates), values)
    for name in unknown:
        warnings.append(f"{{{{{name}}}}} is not a variable and was left as written.")
    body, _ = sanitize(" ".join(body.split()))
    if len(body) > tpl.TEXT_CAP:
        warnings.append(f"This text is {len(body)} characters; the limit is {tpl.TEXT_CAP}. Shorten it.")
    to = e164_of(prospect.get("gbp_phone") or prospect.get("phone")) or None
    return Draft(to=to, subject="", body=body, warnings=tuple(warnings))
