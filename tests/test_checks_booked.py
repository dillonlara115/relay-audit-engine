"""B1 through B7. Fixture driven, no network, no browser.

Two invariants matter more than any single check: the renderer source must have
no code path that submits a form, and every note must state only what was
measured. Both are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.checks import booked  # noqa: F401 - registers the checks
from app.checks.base import REGISTRY, AuditContext
from app.checks.extract import build
from app.scoring import FAIL, PASS, SKIPPED
from app.tools.crawl import FetchResult, SiteCrawl
from app.tools.render import RenderResult

HOST = "https://peakroofing.com"
PLACE = {"business_name": "Peak Roofing", "hours_published": True}


def page(html: str, path: str = "/") -> FetchResult:
    url = f"{HOST}{path}"
    return FetchResult(url=url, final_url=url, status=200, html=html)


def doc(body: str) -> str:
    return f"<html><head><title>Peak</title></head><body>{body}</body></html>"


def context(pages=None, *, homepage_html="<html><body>roofing</body></html>",
            place=None, sitemap=(), form_health=None) -> AuditContext:
    pages = pages or {}
    crawl = SiteCrawl(
        base_url=HOST,
        homepage=page(homepage_html),
        pages={f"{HOST}{p}": page(h, p) for p, h in pages.items()},
        sitemap_urls=[f"{HOST}{p}" for p in sitemap],
    )
    form_render = None
    if form_health is not None:
        form_render = RenderResult(ok=True, url=HOST, form_health=form_health)
    return AuditContext(place={**PLACE, **(place or {})}, site=build(crawl),
                        form_render=form_render)


def run(code, ctx):
    return REGISTRY[code](ctx)


# ── The hard rule ─────────────────────────────────────────────────────────────


def test_the_renderer_has_no_code_path_that_submits_a_form():
    """Hard rule 1. There is no exception and no flag that enables it.

    The probe fills fields and reads checkValidity. If either of these strings
    ever appears in the renderer, that stops being provable from the source.
    """
    import re

    src = Path("renderer/server.js").read_text()
    # The rule is about code. The comment documenting the rule is allowed to
    # name the thing it forbids.
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

    assert ".submit(" not in code, "form.submit() must never exist in the renderer"
    assert "requestSubmit" not in code, "requestSubmit is a submit"
    # Clicking a submit control is a submit too. The probe only queries for it.
    for line in code.splitlines():
        if ".click(" in line:
            raise AssertionError(f"the renderer clicks something: {line.strip()}")


# ── B1 self-serve booking ─────────────────────────────────────────────────────


def test_b1_widget_passes():
    ctx = context(homepage_html=doc('<script src="https://assets.calendly.com/x.js"></script>'))
    res = run("B1", ctx)
    assert res.status == PASS and "calendly" in res.note


def test_b1_booking_path_passes():
    res = run("B1", context(sitemap=("/book-online/",)))
    assert res.status == PASS


def test_b1_booking_words_without_a_scheduler_fail_and_say_why():
    """'Book online' text wired to a plain form is not self serve booking."""
    res = run("B1", context(homepage_html=doc("<a href='/contact'>Book online today</a>")))
    assert res.status == FAIL
    assert "no actual scheduler" in res.note


def test_b1_nothing_fails_with_the_report_sentence():
    res = run("B1", context())
    assert res.status == FAIL
    assert "without waiting for a call back" in res.note


# ── B2 form health ────────────────────────────────────────────────────────────

HEALTHY = {"found": True, "action": "/send", "method": "post", "field_count": 4,
           "required_count": 2, "novalidate": False, "empty_valid": False,
           "filled_valid": True, "has_submit_control": True, "visible": True}


def test_b2_healthy_form_passes_and_says_never_sent():
    res = run("B2", context(form_health=HEALTHY))
    assert res.status == PASS
    assert "never sent" in res.note


def test_b2_accepting_an_empty_submission_is_the_finding():
    res = run("B2", context(form_health={**HEALTHY, "empty_valid": True}))
    assert res.status == FAIL
    assert "empty submission" in res.note


def test_b2_rejecting_a_valid_entry_is_the_expensive_break():
    """The silently broken form: the owner sees nothing, every lead bounces."""
    res = run("B2", context(form_health={**HEALTHY, "filled_valid": False}))
    assert res.status == FAIL
    assert "cannot get through" in res.note


def test_b2_no_submit_control_fails():
    res = run("B2", context(form_health={**HEALTHY, "has_submit_control": False}))
    assert res.status == FAIL


def test_b2_novalidate_passes_with_the_limit_stated():
    """JS-validated forms opt out of browser constraints. We say what we could
    not measure rather than failing them for it."""
    res = run("B2", context(form_health={**HEALTHY, "novalidate": True, "empty_valid": True}))
    assert res.status == PASS
    assert "not measured" in res.note


def test_b2_no_form_anywhere_fails():
    res = run("B2", context(form_health={"found": False}))
    assert res.status == FAIL
    assert "no contact form" in res.note.lower()


def test_b2_unprobed_but_crawled_form_skips():
    form_html = doc('<form action="/s" method="post"><input name="email" required>'
                    '<input name="phone"></form>')
    ctx = context({"/contact/": form_html}, form_health={"found": False})
    ctx.site = build(SiteCrawl(base_url=HOST, homepage=page(form_html)))
    res = run("B2", ctx)
    assert res.status == SKIPPED


# ── B3 text-back ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "html, status",
    [
        ('<script src="https://widget.podium.com/x.js"></script>', PASS),
        ('<a href="sms:+17195550142">Text us</a>', PASS),
        ("<p>Call or text (719) 555-0142</p>", PASS),
        ("<p>Give us a call today.</p>", FAIL),
    ],
)
def test_b3_text_paths(html, status):
    assert run("B3", context(homepage_html=doc(html))).status == status


# ── B4 live chat ──────────────────────────────────────────────────────────────


def test_b4_chat_widget_passes():
    res = run("B4", context(homepage_html=doc('<script src="https://embed.tawk.to/x/1"></script>')))
    assert res.status == PASS


def test_b4_no_chat_fails():
    assert run("B4", context()).status == FAIL


# ── B5 response promise ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, status",
    [
        ("We respond within 30 minutes, guaranteed.", PASS),
        ("You will get a call back within 24 hours.", PASS),
        ("Same-day response on every request.", PASS),
        ("24-hour turnaround on estimates.", PASS),
        ("We will contact you within two business days.", PASS),
        ("We pride ourselves on fast responses.", FAIL),
        ("Fastest roofers in Colorado.", FAIL),
    ],
)
def test_b5_promise_detection(text, status):
    assert run("B5", context(homepage_html=doc(f"<p>{text}</p>"))).status == status


# ── B6 confirmation clarity ───────────────────────────────────────────────────


def test_b6_thankyou_with_next_steps_passes():
    ctx = context({"/thank-you/": doc("<p>Thanks! We will call you within one business day.</p>")})
    res = run("B6", ctx)
    assert res.status == PASS


def test_b6_bare_thanks_fails():
    ctx = context({"/thank-you/": doc("<p>Thank you for your submission.</p>")})
    res = run("B6", ctx)
    assert res.status == FAIL
    assert "does not learn what happens next" in res.note


def test_b6_no_visible_confirmation_skips_because_we_never_submit():
    res = run("B6", context())
    assert res.status == SKIPPED
    assert "never do" in res.note


def test_b6_listed_but_uncrawled_page_skips():
    res = run("B6", context(sitemap=("/thank-you/",)))
    assert res.status == SKIPPED


# ── B7 after-hours ────────────────────────────────────────────────────────────


def test_b7_hours_plus_after_hours_path_passes():
    ctx = context(homepage_html=doc("<p>24/7 emergency service available.</p>"))
    assert run("B7", ctx).status == PASS


def test_b7_hours_without_a_night_path_fails():
    res = run("B7", context())
    assert res.status == FAIL
    assert "9pm" in res.note


def test_b7_neither_fails():
    res = run("B7", context(place={"hours_published": None}))
    assert res.status == FAIL


# ── Notes obey the copy rules ─────────────────────────────────────────────────


def test_booked_notes_state_the_limit_and_never_use_a_dash():
    """CLAUDE.md: booked findings state what we can and cannot see."""
    from app.copy_rules import contains_forbidden_dash

    contexts = [
        context(), context(form_health=HEALTHY),
        context(form_health={**HEALTHY, "empty_valid": True}),
        context(homepage_html=doc("<p>Call or text us. 24/7 emergency.</p>")),
    ]
    for ctx in contexts:
        for code in ("B1", "B2", "B3", "B4", "B5", "B6", "B7"):
            note = run(code, ctx).note
            assert not contains_forbidden_dash(note), (code, note)


def test_b2_will_not_judge_a_popup_form():
    """Regression, found on the rank 1 prospect of the first ranked batch.

    Its only qualifying form lives in a modal. The probe read the hidden
    copy, called it 'rejects a correctly filled entry', and the draft findings
    led with a broken-form claim about a form no homeowner ever touches. A
    verdict about an invisible form is wrong in whichever direction it lands.
    """
    res = run("B2", context(form_health={**HEALTHY, "visible": False, "filled_valid": False}))
    assert res.status == SKIPPED
    assert "popup" in res.note
