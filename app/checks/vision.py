"""C9 and C17. What the vision component saw.

Both skip when there is no verdict. A model that timed out is our problem, and
scoring it as a failure would put an opinion in a report that nothing produced.
"""

from __future__ import annotations

from app.checks.base import AuditContext, CheckResult, check, result, skip

# "Vision verdict of adequate or better", per the criteria doc.
ACCEPTABLE_TRUST = ("strong", "adequate")


def _needs_vision(ctx: AuditContext, code: str) -> CheckResult | None:
    vision = ctx.vision
    if vision is None:
        return skip(code, "The homepage was not looked at, so this was not checked.")
    if not getattr(vision, "ok", False):
        return skip(code, "The homepage could not be assessed.",
                    error=getattr(vision, "error", None))
    return None


@check("C9")
def c9_real_project_photos(ctx: AuditContext) -> CheckResult:
    blocked = _needs_vision(ctx, "C9")
    if blocked:
        return blocked

    vision = ctx.vision
    own_photos = vision.stock_photos is False
    return result(
        "C9", own_photos,
        f"The photographs look like this company's own work. {vision.stock_reason}".strip()
        if own_photos else
        f"The photographs do not look like this company's own work. {vision.stock_reason}".strip(),
        stock_photos=vision.stock_photos,
        reason=vision.stock_reason or None,
    )


@check("C17")
def c17_trust_read(ctx: AuditContext) -> CheckResult:
    """Whether the homepage reads as a real, established local company."""
    blocked = _needs_vision(ctx, "C17")
    if blocked:
        return blocked

    vision = ctx.vision
    verdict = vision.trust_verdict
    ok = verdict in ACCEPTABLE_TRUST
    return result(
        "C17", ok,
        f"A homeowner would read this as a {verdict} first impression. {vision.trust_reason}".strip(),
        trust_verdict=verdict,
        reason=vision.trust_reason or None,
    )
