"""The check registry and runner.

A check is a pure function from an AuditContext to a CheckResult. It returns
pass, fail, skipped, or error, plus what it observed and a plain sentence about
it. It does not know its own point value, because points live in Firestore and
scoring multiplies them in.

Registering by code means the render, speed and vision checks land later in the
week without touching this file or the runner.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from app.checks.extract import SiteFacts
from app.markets import MarketSpec
from app.status import ERROR, FAIL, PASS, SKIPPED

CheckFn = Callable[["AuditContext"], "CheckResult"]

REGISTRY: dict[str, CheckFn] = {}


@dataclass(frozen=True)
class CheckResult:
    code: str
    status: str
    note: str
    observed: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        """Shaped for audits/{auditId}/checks/{code}. Points are added by the
        writer, which knows the definition. Absent stays absent."""
        row: dict[str, Any] = {"code": self.code, "status": self.status, "note": self.note}
        observed = {k: v for k, v in self.observed.items() if v is not None}
        if observed:
            row["observed"] = observed
        return row


@dataclass
class AuditContext:
    """Everything the checks read. Assembled by the audit pipeline."""

    place: Mapping[str, Any]
    site: SiteFacts
    market: MarketSpec | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Filled in later in the week. A check whose input is missing skips itself
    # rather than guessing, which is what keeps `partial` meaningful.
    render: Any = None      # tools.render.RenderResult, homepage
    form_render: Any = None # tools.render.RenderResult, the page carrying the lead form
    psi: Any = None         # tools.pagespeed.PsiResult
    vision: Any = None
    ads: Mapping[str, Any] | None = None

    def field(self, name: str) -> Any:
        return self.place.get(name)


def check(code: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        if code in REGISTRY:
            raise ValueError(f"check {code} is registered twice")
        REGISTRY[code] = fn
        return fn

    return register


# ── Result helpers, so a check body reads as a decision ───────────────────────


def result(code: str, ok: bool, note: str, **observed: Any) -> CheckResult:
    return CheckResult(code=code, status=PASS if ok else FAIL, note=note, observed=observed)


def skip(code: str, note: str, **observed: Any) -> CheckResult:
    return CheckResult(code=code, status=SKIPPED, note=note, observed=observed)


def run_checks(
    ctx: AuditContext,
    definitions: Iterable[Mapping[str, Any]],
) -> dict[str, CheckResult]:
    """Run every enabled definition that has an implementation.

    An enabled check with no implementation is `skipped`, not absent, so the
    score knows the section is thin. A check that raises is `error` with the
    exception recorded, because one bad parse must not lose the other thirty
    nine results.
    """
    out: dict[str, CheckResult] = {}
    for definition in definitions:
        if not definition.get("enabled", True):
            continue
        code = str(definition["code"])
        fn = REGISTRY.get(code)
        if fn is None:
            out[code] = skip(code, "Not implemented yet.")
            continue
        try:
            out[code] = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - one check must not end the audit
            out[code] = CheckResult(
                code=code,
                status=ERROR,
                note=f"Check raised {type(exc).__name__}.",
                observed={"error": f"{type(exc).__name__}: {exc}",
                          "traceback": traceback.format_exc(limit=3)},
            )
    return out


def statuses(results: Mapping[str, CheckResult]) -> dict[str, str]:
    """Shaped for scoring.outcomes_from."""
    return {code: r.status for code, r in results.items()}
