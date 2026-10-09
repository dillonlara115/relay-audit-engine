"""Findings a person writes or rewords.

The model drafts a pool; a person may add a finding of their own (something
seen on a call or in the console that no check measures) or fix the wording of
any finding before it goes out. Both land in the same pool, so a custom finding
can be chosen for the report or held back for a follow-up email like any other.

The same copy rules apply as to a drafted finding, enforced here rather than
trusted: dashes are cleaned, our internal vocabulary is refused (the report
renders these live, so a published page would show an edit at once), and
wording that describes how we measured rather than what a homeowner sees is
flagged for a second look.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.agents.diagnostician import _screen
from app.report.data import forbidden_terms_in

FIELDS = ("what_we_saw", "what_it_means", "what_fixing_takes")
MAX_CHARS = 700
CUSTOM_PREFIX = "X"   # a custom finding's code: X1, X2 ... never a real check


class FindingRejected(ValueError):
    pass


def clean(values: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """The three texts cleaned, and any mechanism words found. Raises
    FindingRejected with a reason a person can act on."""
    out: dict[str, str] = {}
    flags: list[str] = []
    for key in FIELDS:
        text = " ".join(str(values.get(key) or "").split())
        if not text:
            raise FindingRejected("All three parts are needed: what a homeowner sees, what it costs them, what fixing it takes.")
        if len(text) > MAX_CHARS:
            raise FindingRejected(f"Keep each part under {MAX_CHARS} characters.")
        leaked = forbidden_terms_in(text)
        if leaked:
            raise FindingRejected(f"Leave out {', '.join(repr(t) for t in leaked)}: the report never uses those words.")
        cleaned, found, _dirty = _screen(text)
        out[key] = cleaned
        flags.extend(found)
    return out, list(dict.fromkeys(flags))


def _pool(doc: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [dict(f) for f in (doc or {}).get("findings") or []]


def add(doc: Mapping[str, Any] | None, values: Mapping[str, str]) -> list[dict[str, Any]]:
    """The pool with a custom finding added at the end."""
    texts, flags = clean(values)
    pool = _pool(doc)
    ordinal = max((int(f.get("ordinal") or 0) for f in pool), default=0) + 1
    n = sum(1 for f in pool if f.get("custom")) + 1
    while any(f.get("code") == f"{CUSTOM_PREFIX}{n}" for f in pool):
        n += 1
    row: dict[str, Any] = {"code": f"{CUSTOM_PREFIX}{n}", "ordinal": ordinal, "custom": True, **texts}
    if flags:
        row["mechanism_flags"] = flags
    return pool + [row]


def edit(doc: Mapping[str, Any] | None, ordinal: int, values: Mapping[str, str]) -> list[dict[str, Any]]:
    texts, flags = clean(values)
    pool = _pool(doc)
    for row in pool:
        if int(row.get("ordinal") or 0) == ordinal:
            row.update(texts, edited=True)
            if flags:
                row["mechanism_flags"] = flags
            else:
                row.pop("mechanism_flags", None)
            return pool
    raise FindingRejected("That finding is no longer here. Reload the page.")


def remove(doc: Mapping[str, Any] | None, ordinal: int) -> list[dict[str, Any]]:
    """Only a custom finding can be removed, and not while it is on the report."""
    pool = _pool(doc)
    row = next((f for f in pool if int(f.get("ordinal") or 0) == ordinal), None)
    if row is None:
        raise FindingRejected("That finding is no longer here. Reload the page.")
    if not row.get("custom"):
        raise FindingRejected("Only a finding you wrote can be removed. Leave a drafted one unticked instead.")
    if (doc or {}).get("status") == "approved" and ordinal in [int(o) for o in (doc or {}).get("selected") or []]:
        raise FindingRejected("It's one of the three on the report. Choose a different three first.")
    return [f for f in pool if f is not row]


def carry_custom(old: Mapping[str, Any] | None, drafted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A fresh draft keeps the findings a person wrote, renumbered after it."""
    kept = [f for f in _pool(old) if f.get("custom")]
    start = max((int(f.get("ordinal") or 0) for f in drafted), default=0)
    return list(drafted) + [{**f, "ordinal": start + i} for i, f in enumerate(kept, start=1)]
