"""C3 and C4. What PageSpeed Insights measured on a phone.

Both skip when PSI could not answer. A quota error or an unreachable origin is
our problem, not a defect in his website, and scoring it as a failure would put
a number in a report that we did not measure.
"""

from __future__ import annotations

from app.checks.base import AuditContext, CheckResult, check, result, skip
from app.tools.pagespeed import FIELD, MAX_LCP_MS, MIN_PERFORMANCE_SCORE


def _needs_psi(ctx: AuditContext, code: str) -> CheckResult | None:
    psi = ctx.psi
    if psi is None:
        return skip(code, "Page speed was not measured, so this was not checked.")
    if not getattr(psi, "ok", False):
        return skip(code, "Page speed could not be measured.", error=getattr(psi, "error", None))
    return None


@check("C3")
def c3_mobile_speed(ctx: AuditContext) -> CheckResult:
    blocked = _needs_psi(ctx, "C3")
    if blocked:
        return blocked

    score = ctx.psi.performance_score
    if score is None:
        return skip("C3", "Page speed returned no performance score.")

    ok = score >= MIN_PERFORMANCE_SCORE
    return result(
        "C3", ok,
        f"The homepage scores {score} out of 100 for speed on a phone." if ok
        else f"The homepage scores {score} out of 100 for speed on a phone, "
             f"under the {MIN_PERFORMANCE_SCORE} mark.",
        performance_score=score,
    )


@check("C4")
def c4_lcp(ctx: AuditContext) -> CheckResult:
    """Largest Contentful Paint: when the main thing on screen finishes loading.

    The note says whose experience this is. Field data is what real visitors on
    real phones actually waited; lab data is one simulated run. Reporting a lab
    number as if it were his customers' experience would be a claim we did not
    measure.
    """
    blocked = _needs_psi(ctx, "C4")
    if blocked:
        return blocked

    psi = ctx.psi
    lcp = psi.lcp_ms
    if lcp is None:
        return skip("C4", "Page speed returned no load time for the main content.")

    seconds = lcp / 1000.0
    ok = lcp < MAX_LCP_MS
    whose = ("real visitors on phones wait" if psi.lcp_source == FIELD
             else "a test phone waits")

    return result(
        "C4", ok,
        f"The main content finishes loading in {seconds:.1f} seconds, which is what {whose}."
        if ok else
        f"The main content takes {seconds:.1f} seconds to finish loading, which is what "
        f"{whose}. Over {MAX_LCP_MS / 1000:.1f} seconds and people start leaving.",
        lcp_ms=round(lcp),
        lcp_seconds=round(seconds, 2),
        source=psi.lcp_source,
        field_lcp_ms=round(psi.field_lcp_ms) if psi.field_lcp_ms is not None else None,
        lab_lcp_ms=round(psi.lab_lcp_ms) if psi.lab_lcp_ms is not None else None,
        field_category=psi.field_lcp_category,
    )
