"""Brand copy rules that apply to anything a model writes.

CLAUDE.md is explicit that the em-dash ban covers report templates *and model
output*. A model will reach for an em-dash no matter how the prompt is worded,
so the rule is enforced twice: the prompt asks, and this module checks at the
boundary before anything is stored.

Sanitizing silently would hide prompt drift, so `sanitize` reports whether it
had to do anything and callers record that.
"""

from __future__ import annotations

import re

EM_DASH = "—"
EN_DASH = "–"
HORIZONTAL_BAR = "―"
MINUS_SIGN = "−"

FORBIDDEN_DASHES = (EM_DASH, EN_DASH, HORIZONTAL_BAR, MINUS_SIGN)

# " word — word " becomes " word, word ". A dash doing the work of a comma is
# replaced by a comma; a dash doing the work of a full stop reads fine as one.
_SPACED = re.compile(r"\s*[—–―−]\s*")


def contains_forbidden_dash(text: str | None) -> bool:
    return bool(text) and any(dash in text for dash in FORBIDDEN_DASHES)


def sanitize(text: str | None) -> tuple[str, bool]:
    """Return the cleaned text and whether anything had to change."""
    if not text:
        return text or "", False
    if not contains_forbidden_dash(text):
        return text, False
    cleaned = _SPACED.sub(", ", text)
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned).strip()
    return cleaned, True


def assert_clean(text: str | None, where: str = "copy") -> None:
    """Raise rather than ship. Used by the report publish path."""
    if contains_forbidden_dash(text):
        raise ValueError(f"{where} contains a forbidden dash: {text!r}")
