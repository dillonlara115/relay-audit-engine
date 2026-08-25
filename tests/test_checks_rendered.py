"""C2, C5 and C7. Fixture driven over a RenderResult, no browser in the tests.

The renderer returns facts and these checks apply the thresholds, so the
thresholds are testable without launching chromium.
"""

from __future__ import annotations

import pytest

from app.checks import rendered  # noqa: F401 - registers the checks
from app.checks.base import REGISTRY, AuditContext
from app.checks.extract import SiteFacts
from app.scoring import FAIL, PASS, SKIPPED
from app.tools.render import RenderResult


def render(**overrides) -> RenderResult:
    """A render of a competent mobile homepage unless overridden."""
    base = dict(
        ok=True,
        url="https://peakroofing.com/",
        status=200,
        horizontal_scroll=False,
        document={"scroll_width": 390, "client_width": 390},
        fonts={"histogram": {"16": 2000, "24": 300}, "total_chars": 2300},
        tel_links=[{"href": "tel:+17195550142", "text": "(719) 555-0142",
                    "rect": {"top": 20}, "above_fold": True}],
        phone_text=[],
        forms=[{"field_count": 4, "visible": True, "above_fold": True, "rect": {"top": 400}}],
        ctas=[{"text": "Free Estimate", "rect": {"top": 300}, "above_fold": True}],
    )
    base.update(overrides)
    return RenderResult(**base)


def ctx(render_result=None) -> AuditContext:
    return AuditContext(place={}, site=SiteFacts(homepage=None), render=render_result)


def run(code, render_result):
    return REGISTRY[code](ctx(render_result))


# ── Preconditions ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["C2", "C5", "C7"])
def test_no_render_skips(code):
    res = run(code, None)
    assert res.status == SKIPPED
    assert "not rendered" in res.note


@pytest.mark.parametrize("code", ["C2", "C5", "C7"])
def test_a_failed_render_skips_rather_than_failing_the_site(code):
    res = run(code, RenderResult(ok=False, url="https://x.com/", error="Timeout"))
    assert res.status == SKIPPED
    assert "could not be rendered" in res.note


# ── C2 mobile usable ──────────────────────────────────────────────────────────


def test_c2_passes_a_page_that_fits_with_readable_text():
    res = run("C2", render())
    assert res.status == PASS
    assert res.observed["median_font_px"] == 16


def test_c2_fails_on_horizontal_scroll():
    res = run("C2", render(horizontal_scroll=True,
                           document={"scroll_width": 640, "client_width": 390}))
    assert res.status == FAIL
    assert "slides sideways" in res.note and "640px" in res.note


def test_c2_fails_when_the_body_copy_is_small():
    res = run("C2", render(fonts={"histogram": {"13": 2000, "28": 100}, "total_chars": 2100}))
    assert res.status == FAIL
    assert "13px" in res.note


def test_c2_median_ignores_a_giant_headline():
    """One 47px hero must not certify a page whose copy is 13px."""
    res = run("C2", render(fonts={"histogram": {"13": 3000, "47": 400}, "total_chars": 3400}))
    assert res.observed["median_font_px"] == 13
    assert res.status == FAIL


def test_c2_tolerates_a_small_legal_footer():
    """A 10px footer under 16px body copy is normal, not a mobile defect."""
    res = run("C2", render(fonts={"histogram": {"10": 300, "16": 3000}, "total_chars": 3300}))
    assert res.status == PASS
    assert res.observed["median_font_px"] == 16


def test_c2_fails_when_most_text_is_small_even_if_the_median_squeaks_by():
    res = run("C2", render(fonts={"histogram": {"12": 1400, "16": 1500}, "total_chars": 2900}))
    assert res.observed["median_font_px"] == 16, "the median alone would let this through"
    assert res.status == FAIL
    assert "48% of the text is under 14px" in res.note


# Captured from a live render of justroofsandgutters.com on 2026-08-25.
REAL_HISTOGRAM = {"10": 342, "12": 24, "13": 115, "15": 2096, "16": 2433, "18": 177,
                  "19": 96, "22": 22, "24": 164, "27": 55, "28": 30, "40": 172, "47": 15}
REAL_TOTAL_CHARS = 5741


def test_c2_on_a_real_measured_page():
    """A real site mixes 15px and 16px body copy under a 47px hero.

    Counting everything under 16px as small made this 45% small and failed a
    page with no mobile problem. At the 14px line it is 8%.
    """
    res = run("C2", render(fonts={"histogram": REAL_HISTOGRAM, "total_chars": REAL_TOTAL_CHARS}))
    assert res.observed["median_font_px"] == 16
    assert res.observed["small_text_ratio"] == pytest.approx(0.084, abs=0.005)
    assert res.status == PASS


def test_c2_holds_the_sixteen_pixel_line_on_the_median():
    """The criteria doc says 16px+, so a 15px median is a fail and not a
    rounding allowance. Softening this is a threshold decision, not a bug."""
    res = run("C2", render(fonts={"histogram": {"15": 3000, "24": 200}, "total_chars": 3200}))
    assert res.observed["median_font_px"] == 15
    assert res.status == FAIL


def test_c2_with_no_text_skips():
    res = run("C2", render(fonts={"histogram": {}, "total_chars": 0}))
    assert res.status == SKIPPED


# ── C5 phone above fold ───────────────────────────────────────────────────────


def test_c5_passes_when_the_number_is_on_the_first_screen():
    res = run("C5", render())
    assert res.status == PASS
    assert "(719) 555-0142" in res.note


def test_c5_fails_when_the_number_is_below_the_fold_and_says_how_far():
    """Found on a real site: the header number lives in a closed hamburger menu,
    so the first number a homeowner can actually see is in the footer."""
    res = run("C5", render(tel_links=[{"href": "tel:+17195550142", "text": "(719) 555-0142",
                                       "rect": {"top": 11775}, "above_fold": False}]))
    assert res.status == FAIL
    assert "11775px down" in res.note


def test_c5_fails_when_there_is_no_number_at_all():
    res = run("C5", render(tel_links=[], phone_text=[]))
    assert res.status == FAIL
    assert "anywhere on the first screen" in res.note


def test_c5_accepts_plain_text_numbers_not_just_tel_links():
    res = run("C5", render(tel_links=[], phone_text=[
        {"text": "(719) 555-0142", "rect": {"top": 30}, "above_fold": True}
    ]))
    assert res.status == PASS


# ── C7 form or CTA above fold ─────────────────────────────────────────────────


def test_c7_passes_on_a_form_above_the_fold():
    res = run("C7", render(ctas=[]))
    assert res.status == PASS
    assert "contact form is on the first screen" in res.note


def test_c7_falls_back_to_a_primary_cta():
    res = run("C7", render(forms=[{"field_count": 4, "visible": True, "above_fold": False,
                                   "rect": {"top": 2000}}]))
    assert res.status == PASS
    assert "Free Estimate" in res.note


def test_c7_fails_when_the_first_screen_offers_no_next_step():
    res = run("C7", render(
        forms=[{"field_count": 4, "visible": True, "above_fold": False, "rect": {"top": 2000}}],
        ctas=[{"text": "Free Estimate", "rect": {"top": 2400}, "above_fold": False}],
    ))
    assert res.status == FAIL
    assert res.observed["forms_on_page"] == 1
    assert res.observed["ctas_on_page"] == 1


# ── RenderResult derived reads ────────────────────────────────────────────────


def test_median_and_ratio_on_an_empty_histogram_are_none():
    empty = RenderResult(ok=True, url="https://x.com/", fonts={}, )
    assert empty.median_font_px() is None
    assert empty.small_text_ratio() is None


def test_small_text_ratio_counts_only_below_sixteen():
    result = render(fonts={"histogram": {"12": 250, "16": 750}, "total_chars": 1000})
    assert result.small_text_ratio() == pytest.approx(0.25)


def test_screenshot_decodes_from_base64():
    import base64

    payload = b"\x89PNG fake"
    result = render(screenshot_b64=base64.b64encode(payload).decode())
    assert result.screenshot() == payload
    assert render().screenshot() is None
