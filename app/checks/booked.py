"""The Booked section. B1 through B7, the 40 points nobody else audits.

We cannot see how fast his team moves. We can see whether anything is set up to
catch a lead at all, and the honest framing in every note reflects that: the
tools exist or they do not, and that is all we measured.

B2 is the sleeper and the one with a hard rule attached: the form is filled and
its validity read, never submitted. The renderer has no code path that submits,
and this module only reads what the renderer observed.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.checks.base import AuditContext, CheckResult, check, result, skip

# ── B1 self-serve booking ─────────────────────────────────────────────────────

BOOKING_WIDGETS = (
    "calendly.com", "acuityscheduling", "squarespacescheduling", "youcanbook.me",
    "setmore.com", "appointlet", "simplybook", "squareup.com/appointments",
    "scheduleengine", "servicetitan.com/schedule", "book.housecallpro",
    "housecallpro.com/book", "getjobber.com/booking", "clienthub.getjobber",
    "workiz.com/book", "bookingkoala", "calendar.app.google", "hubspot.com/meetings",
    "meetings.hubspot",
)
BOOKING_PATHS = ("book-online", "book-now", "schedule-online", "schedule-now",
                 "book-appointment", "schedule-appointment", "online-booking",
                 "self-schedule", "instant-quote", "instant-roofing-pricing")
BOOKING_PHRASES = ("book online", "book your inspection online", "schedule online",
                   "pick a time", "choose a time", "book an appointment online",
                   "schedule your appointment online", "instant quote")

# ── B3 missed-call text-back ──────────────────────────────────────────────────

TEXTBACK_FINGERPRINTS = ("podium.com", "widget.podium", "hatchapp", "usehatchapp",
                         "textrequest", "text-request", "zipwhip", "skipio",
                         "kenect.com", "chiirp", "salesmsg")
TEXT_PHRASES = ("text us", "call or text", "text or call", "send us a text",
                "text for a quote", "text us at")

# ── B4 live chat ──────────────────────────────────────────────────────────────

CHAT_WIDGETS = ("tawk.to", "livechatinc", "livechat.com", "intercom.io", "intercomcdn",
                "drift.com", "driftt.com", "tidio", "crisp.chat", "smartsupp",
                "zopim", "zendesk", "chatra", "purechat", "olark", "hubspot",
                "facebook.com/plugins/customerchat", "connect.facebook.net/en_us/sdk/xfbml.customerchat",
                "podium.com", "birdeye.com", "signpost.com", "gorgias")

# ── B5 response promise ───────────────────────────────────────────────────────

_PROMISE = re.compile(
    r"(?:respond|reply|call(?:ed)?\s+(?:you\s+)?back|get\s+back\s+to\s+you|contact\s+you|follow\s+up)"
    r"[^.!?]{0,40}?within\s+(?:one|two|24|48|\d{1,3})\s*(?:minutes?|min|hours?|hrs?|business\s+(?:hours?|days?)|days?)"
    r"|within\s+(?:one|two|24|48|\d{1,3})\s*(?:minutes?|min|hours?|hrs?)[^.!?]{0,40}?"
    r"(?:respond|reply|call|quote|estimate|get\s+back)"
    r"|same[- ]day\s+(?:response|reply|call|quote)"
    r"|(?:24|48)[- ]hour\s+(?:response|reply|turnaround|quote|callback|call[- ]back)",
    re.I,
)

# ── B6 confirmation clarity ───────────────────────────────────────────────────

THANKYOU_PATHS = ("thank-you", "thankyou", "thanks", "confirmation", "form-success",
                  "success")
NEXT_STEP_PHRASES = ("we will call", "we'll call", "we will contact", "we'll contact",
                     "we will reach out", "we'll reach out", "we will be in touch",
                     "we'll be in touch", "expect a call", "expect to hear",
                     "within one business", "within 24", "within 48", "next step",
                     "what happens next", "hear from us", "get back to you")

# ── B7 after-hours ────────────────────────────────────────────────────────────

AFTER_HOURS_PHRASES = ("24/7", "24-7", "24 hours a day", "around the clock",
                       "after hours", "after-hours", "emergency service",
                       "emergency roof", "emergency repair", "night or day",
                       "any time day or night", "nights and weekends")


def _unreachable(ctx: AuditContext, code: str) -> CheckResult | None:
    if ctx.site.robots_blocked and not ctx.site.pages:
        return skip(code, "The site disallows crawling, so this was not checked.")
    if not ctx.site.reachable or not ctx.site.pages:
        return skip(code, "No page could be read from the site.")
    return None


def _first_in(haystack: str, needles: Iterable[str]) -> str | None:
    for needle in needles:
        if needle in haystack:
            return needle
    return None


@check("B1")
def b1_self_serve_booking(ctx: AuditContext) -> CheckResult:
    """Can a homeowner pick a time without waiting for a human?"""
    blocked = _unreachable(ctx, "B1")
    if blocked:
        return blocked

    widget = _first_in(ctx.site.html, BOOKING_WIDGETS)
    path = next((p for p in ctx.site.all_paths
                 if any(b in p.lower() for b in BOOKING_PATHS)), None)
    phrase = _first_in(ctx.site.text, BOOKING_PHRASES)

    ok = bool(widget or path)
    if ok:
        how = f"a {widget} scheduler" if widget else f"a booking page at {path}"
        return result("B1", True, f"A homeowner can pick a time without waiting: {how}.",
                      widget=widget, booking_path=path, phrase=phrase)
    if phrase:
        # Words without a mechanism. "Book online" text that leads to a form is
        # not self serve booking, and crediting it would overstate the finding.
        return result("B1", False,
                      f"The site says {phrase!r} but no actual scheduler is wired to it. "
                      "Nobody can pick a time without waiting for a call back.",
                      phrase=phrase)
    return result("B1", False,
                  "Nobody can book a time without waiting for a call back.")


@check("B2")
def b2_form_health(ctx: AuditContext) -> CheckResult:
    """The sleeper. A silently broken form is invisible to the owner.

    Measured by filling, never submitting. Three facts decide it: the form
    exists, empty submissions are refused, and a filled form is accepted.
    """
    blocked = _unreachable(ctx, "B2")
    if blocked:
        return blocked

    render = ctx.form_render or ctx.render
    health = getattr(render, "form_health", None) if render else None

    if health is None or not health.get("found"):
        # No probed form. If the crawl saw no form either, that is a fail the
        # homeowner experiences. If the crawl saw one the renderer could not
        # probe, we did not measure it.
        if not ctx.site.forms:
            return result("B2", False, "There is no contact form anywhere on the site.")
        return skip("B2", "The contact form could not be probed in a browser.")

    if not health.get("visible"):
        # The only qualifying form sits in a popup or hidden layer. We do not
        # open popups, and a verdict about a form no visitor can see is a false
        # finding in whichever direction it lands. Measured on a real prospect:
        # the hidden modal read as "rejects a correctly filled entry" and put a
        # broken-form claim on a form a homeowner never touches.
        return skip("B2", "The contact form opens in a popup the probe does not open, "
                          "so its behavior was not checked.",
                    visible=False, field_count=health.get("field_count"))

    problems = []
    if not health.get("has_submit_control"):
        problems.append("the form has no working send button")
    if health.get("empty_valid") and not health.get("novalidate"):
        problems.append("it accepts a completely empty submission, so broken and "
                        "spam entries go straight through")
    if not health.get("filled_valid") and not health.get("novalidate"):
        problems.append("it rejects a correctly filled entry, so a real homeowner "
                        "cannot get through")

    observed = {
        "action": health.get("action"),
        "method": health.get("method"),
        "field_count": health.get("field_count"),
        "required_count": health.get("required_count"),
        "novalidate": health.get("novalidate"),
        "empty_valid": health.get("empty_valid"),
        "filled_valid": health.get("filled_valid"),
        "has_submit_control": health.get("has_submit_control"),
        "visible": health.get("visible"),
        "invalid_fields": health.get("invalid_fields") or None,
    }

    if problems:
        return result("B2", False,
                      "The contact form was filled and checked, never sent: "
                      + "; ".join(problems) + ".", **observed)

    if health.get("novalidate"):
        return result("B2", True,
                      "The contact form accepts a correctly filled entry. Its checks run "
                      "in scripts we cannot exercise, so empty-entry handling was not measured.",
                      **observed)
    return result("B2", True,
                  "The contact form was filled and checked, never sent: it refuses an "
                  "empty entry and accepts a complete one.", **observed)


@check("B3")
def b3_text_back(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "B3")
    if blocked:
        return blocked

    vendor = _first_in(ctx.site.html, TEXTBACK_FINGERPRINTS)
    sms = "sms:" in ctx.site.html
    phrase = _first_in(ctx.site.text, TEXT_PHRASES)

    ok = bool(vendor or sms or phrase)
    if ok:
        how = (f"a {vendor} texting tool" if vendor
               else "a tap-to-text link" if sms
               else f"the site invites texting ({phrase!r})")
        return result("B3", True, f"A missed call has a text path: {how}.",
                      vendor=vendor, sms_link=sms or None, phrase=phrase)
    return result("B3", False,
                  "A caller who gets voicemail has no text path back. The lead is "
                  "gone the moment they hang up.")


@check("B4")
def b4_live_chat(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "B4")
    if blocked:
        return blocked
    widget = _first_in(ctx.site.html, CHAT_WIDGETS)
    if widget:
        return result("B4", True, f"A chat window is available ({widget}).", widget=widget)
    return result("B4", False, "There is no way to start a conversation from the page.")


@check("B5")
def b5_response_promise(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "B5")
    if blocked:
        return blocked
    match = _PROMISE.search(ctx.site.text)
    if match:
        quoted = re.sub(r"\s+", " ", match.group(0)).strip()
        return result("B5", True, f"The site commits to a response time: {quoted!r}.",
                      promise=quoted)
    return result("B5", False,
                  "Nothing on the site says how fast anyone will get back to a homeowner.")


@check("B6")
def b6_confirmation_clarity(ctx: AuditContext) -> CheckResult:
    """Does the thank-you state say what happens next?

    Only measurable when the site exposes a thank-you page we can read without
    submitting. An inline-only confirmation is invisible to us by design, and
    the note says so rather than guessing.
    """
    blocked = _unreachable(ctx, "B6")
    if blocked:
        return blocked

    page = next((p for p in ctx.site.pages
                 if any(t in p.path.lower() for t in THANKYOU_PATHS)), None)
    if page is None:
        listed = next((p for p in ctx.site.all_paths
                       if any(t in p.lower() for t in THANKYOU_PATHS)), None)
        if listed:
            return skip("B6", f"A confirmation page exists at {listed} but was not crawled.",
                        thankyou_path=listed)
        return skip("B6", "No confirmation page is visible without sending the form, "
                          "which we never do.")

    phrase = _first_in(page.text, NEXT_STEP_PHRASES)
    if phrase:
        return result("B6", True,
                      f"After the form, the page says what happens next ({phrase!r}).",
                      thankyou_path=page.path, phrase=phrase)
    return result("B6", False,
                  "The confirmation page says thanks and nothing else. A homeowner "
                  "does not learn what happens next or when.",
                  thankyou_path=page.path)


@check("B7")
def b7_after_hours(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "B7")
    if blocked:
        return blocked

    hours_published = ctx.field("hours_published")
    phrase = _first_in(ctx.site.text, AFTER_HOURS_PHRASES)

    if hours_published and phrase:
        return result("B7", True,
                      f"Hours are on the profile and the site states an after-hours path "
                      f"({phrase!r}).", hours_published=True, phrase=phrase)
    if phrase and hours_published is None:
        return result("B7", False,
                      f"The site mentions {phrase!r} but the Google profile publishes no "
                      "hours, so a homeowner cannot tell when calling works.",
                      phrase=phrase)
    if hours_published and not phrase:
        return result("B7", False,
                      "Hours are published but nothing says what happens to a leak at "
                      "9pm. Storm damage does not keep business hours.",
                      hours_published=True)
    return result("B7", False,
                  "No published hours and no after-hours path. A homeowner with an "
                  "urgent leak has no idea if anyone will answer.")
