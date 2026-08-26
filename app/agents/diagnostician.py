"""The diagnostician. Drafts the three findings a report is built around.

The model drafts, the human approves. Rule 7 is absolute: findings are never
auto-selected by score rank, so everything this module produces is stored as a
draft that an operator must approve before any report exists. What the model is
for is the part scoring cannot do: judging which three failures cost the most
booked jobs, and saying so in a homeowner's language.

Brand rules enforced here, not just asked for in the prompt:
- exactly three findings, asserted at parse time
- no em-dashes, sanitized at the boundary
- no mechanism language, screened and flagged for the approving human
- no numbers the audit did not measure
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.config import get_config
from app.copy_rules import sanitize

FINDINGS_REQUIRED = 3

PROMPT = """You are looking at the audit of a residential roofing contractor's online presence.
Below are the checks that failed, with what the auditor saw. Internal notes may name
tools and mechanisms; your output NEVER does.

Pick exactly the three failures that cost this business the most booked jobs, ranked
by lost revenue, not by how easy they are to fix. For each, write three short plain
sentences for the owner:

- what_we_saw: what a homeowner looking for a roofer actually experiences. Outcome
  language only. Never name a tool, platform, tag, script, schema, widget, or metric.
  Say "nobody can book a time without waiting for a call back", not "no scheduling
  widget detected".
- what_it_means: the cost in missed calls, lost trust, or jobs that go to a
  competitor. Never invent a number. If the audit measured a number you may use it.
- what_fixing_takes: the size of the fix in plain terms (an afternoon, a small
  change to the site, a service his office can turn on). No vendor names.

Rules: never use an em-dash or en-dash. Never mention scores, points, bands, or
segments. Write as if the owner will read this over coffee.

Business: {business_name}, {city}

Failed checks:
{failures}
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "findings": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "check_code": {"type": "STRING"},
                    "what_we_saw": {"type": "STRING"},
                    "what_it_means": {"type": "STRING"},
                    "what_fixing_takes": {"type": "STRING"},
                },
                "required": ["check_code", "what_we_saw", "what_it_means", "what_fixing_takes"],
            },
        }
    },
    "required": ["findings"],
}

# Words that mean the draft is describing our tooling rather than his business.
# A hit does not reject the draft; it flags it for the human who must approve
# it anyway, because a false positive here ("we searched Google") is possible.
_MECHANISM_WORDS = re.compile(
    r"\b(schema|widget|plugin|api|json|markup|metadata|meta tag|lighthouse|pagespeed|"
    r"psi|seo|serp|crawl|render|pixel|tracking tag|analytics|tel:|href|dom|cms|"
    r"wordpress|javascript|lcp|viewport)\b",
    re.I,
)

_SCORE_WORDS = re.compile(r"\b(score|points?|band|segment)\b", re.I)


@dataclass(frozen=True)
class Finding:
    check_code: str
    what_we_saw: str
    what_it_means: str
    what_fixing_takes: str
    ordinal: int
    mechanism_flags: tuple[str, ...] = ()
    sanitized: bool = False

    def to_dict(self) -> dict[str, Any]:
        row = {
            "code": self.check_code,
            "ordinal": self.ordinal,
            "what_we_saw": self.what_we_saw,
            "what_it_means": self.what_it_means,
            "what_fixing_takes": self.what_fixing_takes,
        }
        if self.mechanism_flags:
            row["mechanism_flags"] = list(self.mechanism_flags)
        if self.sanitized:
            row["sanitized"] = True
        return row


@dataclass(frozen=True)
class Diagnosis:
    ok: bool
    findings: tuple[Finding, ...] = ()
    model: str | None = None
    error: str | None = None
    needs_review: bool = False


def _screen(text: str) -> tuple[str, list[str], bool]:
    cleaned, dirty = sanitize(text.strip())
    flags = sorted({m.group(0).lower() for m in _MECHANISM_WORDS.finditer(cleaned)})
    flags += sorted({m.group(0).lower() for m in _SCORE_WORDS.finditer(cleaned)})
    return cleaned, flags, dirty


def parse_diagnosis(raw: Any, *, valid_codes: Sequence[str], model: str | None = None) -> Diagnosis:
    """Validate the model's draft. Exactly three findings or nothing."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            return Diagnosis(ok=False, model=model, error=f"unparseable JSON: {exc}")
    findings_raw = (raw or {}).get("findings") if isinstance(raw, dict) else None
    if not isinstance(findings_raw, list):
        return Diagnosis(ok=False, model=model, error="response carried no findings list")

    # Exactly three. Not "up to three", not "the best four". Enforced by code
    # because a prompt is a request and this is a rule.
    if len(findings_raw) != FINDINGS_REQUIRED:
        return Diagnosis(ok=False, model=model,
                         error=f"expected {FINDINGS_REQUIRED} findings, got {len(findings_raw)}")

    valid = set(valid_codes)
    findings: list[Finding] = []
    any_flags = False
    for ordinal, row in enumerate(findings_raw, start=1):
        if not isinstance(row, dict):
            return Diagnosis(ok=False, model=model, error=f"finding {ordinal} was not an object")
        code = str(row.get("check_code") or "").strip().upper()
        if code not in valid:
            return Diagnosis(ok=False, model=model,
                             error=f"finding {ordinal} cites {code!r}, which did not fail")
        texts = {}
        flags: list[str] = []
        dirty = False
        for key in ("what_we_saw", "what_it_means", "what_fixing_takes"):
            value = str(row.get(key) or "")
            if not value.strip():
                return Diagnosis(ok=False, model=model, error=f"finding {ordinal} has an empty {key}")
            cleaned, found_flags, was_dirty = _screen(value)
            texts[key] = cleaned
            flags.extend(found_flags)
            dirty = dirty or was_dirty
        any_flags = any_flags or bool(flags)
        findings.append(Finding(check_code=code, ordinal=ordinal,
                                mechanism_flags=tuple(dict.fromkeys(flags)),
                                sanitized=dirty, **texts))

    codes = [f.check_code for f in findings]
    if len(set(codes)) != FINDINGS_REQUIRED:
        return Diagnosis(ok=False, model=model, error=f"duplicate finding codes: {codes}")

    return Diagnosis(ok=True, findings=tuple(findings), model=model, needs_review=any_flags)


def _failures_block(failures: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for row in failures:
        note = str(row.get("note") or "")
        lines.append(f"- {row.get('code')} ({row.get('title')}, {row.get('points')} pts): {note}")
    return "\n".join(lines)


async def draft_findings(
    *,
    business_name: str,
    city: str,
    failures: Sequence[Mapping[str, Any]],
) -> Diagnosis:
    """Ask the model to pick three and write consequences. Never raises."""
    cfg = get_config()
    if len(failures) < FINDINGS_REQUIRED:
        return Diagnosis(ok=False,
                         error=f"only {len(failures)} failed checks, three findings need three failures")

    prompt = PROMPT.format(
        business_name=business_name, city=city or "Colorado",
        failures=_failures_block(failures),
    )
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(vertexai=cfg.use_vertexai, project=cfg.project,
                              location=cfg.model_location)
        response = await client.aio.models.generate_content(
            model=cfg.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.2,
                # Reasoning off, as with vision: measured to give the same
                # selections for a fraction of the tokens on this shaped task.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=1200,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a model fault is a missing draft, not a crash
        return Diagnosis(ok=False, model=cfg.gemini_model,
                         error=f"{type(exc).__name__}: {exc}"[:300])

    finish = getattr((response.candidates or [None])[0], "finish_reason", None)
    if finish is not None and str(finish).endswith("MAX_TOKENS"):
        return Diagnosis(ok=False, model=cfg.gemini_model, error="response truncated")

    return parse_diagnosis(response.text, valid_codes=[str(f.get("code")) for f in failures],
                           model=cfg.gemini_model)
