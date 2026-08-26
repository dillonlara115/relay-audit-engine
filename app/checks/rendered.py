"""Checks that need a real browser at a real mobile viewport.

C2, C5 and C7 are all questions about what a homeowner sees before scrolling,
which no amount of HTML parsing can answer. B2 and B6 join them on Friday.

Every check here skips when the render is missing rather than failing, because
"our renderer was down" is not a defect in his website.
"""

from __future__ import annotations

from app.checks.base import AuditContext, CheckResult, check, result, skip
from app.tools.render import MIN_BODY_FONT_PX

# The median catches a site whose body copy is genuinely small. This second rule
# catches the other shape: a median that scrapes over 16px while half the page
# is set far below it.
#
# The bar is 14px, not 16px, on purpose. Measured across real roofing sites,
# 15px body copy is common and reads fine on a phone, so counting it as "small"
# would fail sites that have no mobile problem. 12px and below is a squint.
SMALL_TEXT_PX = 14
MAX_SMALL_TEXT_RATIO = 0.35


def _needs_render(ctx: AuditContext, code: str) -> CheckResult | None:
    render = ctx.render
    if render is None:
        return skip(code, "The page was not rendered, so this was not checked.")
    if not getattr(render, "ok", False):
        return skip(code, "The page could not be rendered in a browser.",
                    error=getattr(render, "error", None))
    challenge = render.bot_challenge()
    if challenge:
        # His site is fine. Ours got stopped at the door, and scoring the door
        # would be scoring the wrong page.
        return skip(code, "The site showed a bot protection screen instead of the page, "
                          "so this was not checked.", challenge=challenge)
    return None


@check("C2")
def c2_mobile_usable(ctx: AuditContext) -> CheckResult:
    blocked = _needs_render(ctx, "C2")
    if blocked:
        return blocked
    render = ctx.render

    scrolls = bool(render.horizontal_scroll)
    median = render.median_font_px()
    small_ratio = render.small_text_ratio(below=SMALL_TEXT_PX)

    problems = []
    if scrolls:
        width = (render.document or {}).get("scroll_width")
        client = (render.document or {}).get("client_width")
        problems.append(
            f"the page is {width}px wide on a {client}px screen, so it slides sideways"
        )
    if median is not None and median < MIN_BODY_FONT_PX:
        problems.append(f"most of the text is set at {median}px")
    elif small_ratio is not None and small_ratio > MAX_SMALL_TEXT_RATIO:
        problems.append(f"{small_ratio:.0%} of the text is under {SMALL_TEXT_PX}px")

    observed = {
        "horizontal_scroll": scrolls,
        "median_font_px": median,
        "small_text_ratio": round(small_ratio, 3) if small_ratio is not None else None,
        "overflowing": list(render.overflowing_elements)[:3] or None,
    }

    if median is None and not scrolls:
        return skip("C2", "No readable text was found on the rendered page.", **observed)
    if problems:
        return result("C2", False, "On a phone, " + " and ".join(problems) + ".", **observed)
    return result("C2", True,
                  f"On a phone the page fits the screen and the text is set at {median}px.",
                  **observed)


@check("C5")
def c5_phone_above_fold(ctx: AuditContext) -> CheckResult:
    blocked = _needs_render(ctx, "C5")
    if blocked:
        return blocked
    render = ctx.render

    visible = render.phones_above_fold()
    if visible:
        shown = (visible[0].get("text") or visible[0].get("href") or "").strip()
        return result("C5", True, f"The phone number {shown} is visible without scrolling.",
                      above_fold=[v.get("text") for v in visible][:3])

    anywhere = list(render.tel_links) + list(render.phone_text)
    if anywhere:
        first = anywhere[0]
        top = (first.get("rect") or {}).get("top")
        return result(
            "C5", False,
            "The phone number is on the page but not on the first screen, "
            + (f"about {top}px down." if isinstance(top, int) else "further down the page."),
            first_phone=first.get("text"), first_phone_top_px=top,
        )
    return result("C5", False, "No phone number appears anywhere on the first screen.")


@check("C7")
def c7_cta_above_fold(ctx: AuditContext) -> CheckResult:
    """A form above the fold, or one visible primary call to action."""
    blocked = _needs_render(ctx, "C7")
    if blocked:
        return blocked
    render = ctx.render

    forms = render.forms_above_fold()
    if forms:
        return result("C7", True,
                      f"A contact form is on the first screen, asking for "
                      f"{forms[0].get('field_count')} pieces of information.",
                      form_field_count=forms[0].get("field_count"))

    ctas = render.ctas_above_fold()
    if ctas:
        label = (ctas[0].get("text") or "").strip()
        return result("C7", True, f"A clear next step is on the first screen: {label!r}.",
                      cta=label, cta_count=len(ctas))

    return result("C7", False,
                  "Nothing on the first screen invites a homeowner to take a next step.",
                  forms_on_page=len(render.forms), ctas_on_page=len(render.ctas))
