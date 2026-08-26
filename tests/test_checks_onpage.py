"""On-page checks. Fixture driven, no network, per engine spec section 10.

Every check gets a passing case and a failing case. The ones that can be wrong
in an expensive way (F5, F7, F15, C8) get their edge cases too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.checks import onpage  # noqa: F401 - registers the checks
from app.checks.base import REGISTRY, AuditContext, run_checks, statuses
from app.checks.extract import build
from app.markets import COLORADO_SPRINGS
from app.scoring import FAIL, PASS, SKIPPED
from app.tools.crawl import FetchResult, SiteCrawl

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
HOST = "https://peakroofing.com"

PLACE = {
    "business_name": "Peak Roofing",
    "primary_type": "roofing_contractor",
    "review_count": 120,
    "rating": 4.8,
    "gbp_phone": "(719) 555-0142",
    "latest_review_at": NOW - timedelta(days=5),
    "review_sample_size": 5,
}


def page(html: str, path: str = "/") -> FetchResult:
    url = f"{HOST}{path}"
    return FetchResult(url=url, final_url=url, status=200, html=html)


def context(pages: dict[str, str] | None = None, *, place: dict | None = None,
            sitemap: tuple[str, ...] = (), lastmod: dict | None = None,
            robots_blocked: tuple[str, ...] = (), homepage_html: str | None = None,
            market=COLORADO_SPRINGS) -> AuditContext:
    pages = pages or {}
    home = page(homepage_html if homepage_html is not None else pages.pop("/", "<html><body></body></html>"))
    crawl = SiteCrawl(
        base_url=HOST,
        homepage=home,
        pages={f"{HOST}{p}": page(h, p) for p, h in pages.items()},
        sitemap_urls=[f"{HOST}{p}" for p in sitemap],
        sitemap_lastmod={f"{HOST}{p}": d for p, d in (lastmod or {}).items()},
        robots_blocked=list(robots_blocked),
    )
    return AuditContext(
        place={**PLACE, **(place or {})}, site=build(crawl), market=market, now=NOW
    )


def run(code: str, ctx: AuditContext):
    return REGISTRY[code](ctx)


def doc(body: str, head: str = "") -> str:
    return f"<html><head><title>Peak Roofing</title>{head}</head><body>{body}</body></html>"


def ld(payload: str) -> str:
    return f'<script type="application/ld+json">{payload}</script>'


# ── FOUND ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "primary, status",
    [("roofing_contractor", PASS), ("general_contractor", FAIL), (None, SKIPPED)],
)
def test_f2_primary_category(primary, status):
    assert run("F2", context(place={"primary_type": primary})).status == status


@pytest.mark.parametrize("count, status", [(50, PASS), (120, PASS), (49, FAIL), (None, SKIPPED)])
def test_f3_review_count(count, status):
    assert run("F3", context(place={"review_count": count})).status == status


@pytest.mark.parametrize("rating, status", [(4.5, PASS), (5.0, PASS), (4.4, FAIL), (None, SKIPPED)])
def test_f4_rating(rating, status):
    assert run("F4", context(place={"rating": rating})).status == status


def test_f5_recent_review_passes():
    res = run("F5", context(place={"latest_review_at": NOW - timedelta(days=10)}))
    assert res.status == PASS
    assert "10 days old" in res.note


def test_f5_stale_but_truncated_sample_skips_rather_than_accusing():
    """Places returns five reviews and does not promise they are the newest.

    Telling a roofer his reviews dried up when they did not is the expensive
    mistake here, so an inconclusive sample is inconclusive.
    """
    res = run("F5", context(place={
        "latest_review_at": NOW - timedelta(days=200), "review_sample_size": 5, "review_count": 120,
    }))
    assert res.status == SKIPPED
    assert "sample of 120" in res.note


def test_f5_stale_with_a_complete_history_fails():
    res = run("F5", context(place={
        "latest_review_at": NOW - timedelta(days=200), "review_sample_size": 30, "review_count": 30,
    }))
    assert res.status == FAIL


def test_f5_without_timestamps_skips():
    assert run("F5", context(place={"latest_review_at": None})).status == SKIPPED


def test_f7_matching_phone_passes():
    res = run("F7", context(homepage_html=doc('<a href="tel:+17195550142">Call</a>')))
    assert res.status == PASS


def test_f7_call_tracking_swap_is_the_finding():
    res = run("F7", context(homepage_html=doc('<a href="tel:+18445559900">Call</a>')))
    assert res.status == FAIL
    assert "(844) 555-9900" in res.note and "(719) 555-0142" in res.note


def test_f7_passes_when_the_profile_number_appears_alongside_another():
    """A tracking number is fine as long as the profile number is also reachable."""
    res = run("F7", context(homepage_html=doc(
        '<a href="tel:+18445559900">Call</a><a href="tel:+17195550142">Office</a>'
    )))
    assert res.status == PASS


def test_f7_with_no_site_number_fails():
    assert run("F7", context(homepage_html=doc("<p>Contact us</p>"))).status == FAIL


def test_f7_without_a_profile_number_skips():
    assert run("F7", context(place={"gbp_phone": None})).status == SKIPPED


def test_f10_counts_city_pages_from_the_sitemap():
    res = run("F10", context(sitemap=("/roofing-monument/", "/roofing-fountain/", "/roofing-falcon/")))
    assert res.status == PASS
    assert res.observed["city_page_count"] == 3


def test_f10_below_three_fails():
    res = run("F10", context(sitemap=("/roofing-monument/", "/roofing-fountain/")))
    assert res.status == FAIL


def test_f11_fresh_content_passes():
    res = run("F11", context(homepage_html=doc(
        "<p>hi</p>", head='<meta property="article:modified_time" content="2026-07-01T00:00:00Z">'
    )))
    assert res.status == PASS


def test_f11_uses_sitemap_lastmod():
    res = run("F11", context(sitemap=("/blog/",), lastmod={"/blog/": NOW - timedelta(days=30)}))
    assert res.status == PASS


def test_f11_stale_content_fails():
    res = run("F11", context(homepage_html=doc(
        "<p>hi</p>", head='<meta property="article:modified_time" content="2024-01-01T00:00:00Z">'
    )))
    assert res.status == FAIL


def test_f11_with_no_dates_at_all_skips():
    assert run("F11", context(homepage_html=doc("<p>hi</p>"))).status == SKIPPED


def test_f14_review_schema_passes():
    res = run("F14", context(homepage_html=doc("", head=ld(
        '{"@type":"AggregateRating","ratingValue":"4.8","reviewCount":"120"}'
    ))))
    assert res.status == PASS


def test_f14_without_review_schema_fails():
    res = run("F14", context(homepage_html=doc("", head=ld('{"@type":"LocalBusiness","name":"Peak"}'))))
    assert res.status == FAIL


def test_f14_accepts_microdata():
    res = run("F14", context(homepage_html=doc(
        '<div itemscope itemtype="https://schema.org/Review">great</div>'
    )))
    assert res.status == PASS


LOCAL_BUSINESS = ld(
    '{"@type":"RoofingContractor","name":"Peak Roofing",'
    '"address":{"@type":"PostalAddress","streetAddress":"1200 Garden of the Gods Rd"},'
    '"telephone":"(719) 555-0142"}'
)


def test_f15_complete_business_schema_passes():
    assert run("F15", context(homepage_html=doc("", head=LOCAL_BUSINESS))).status == PASS


def test_f15_no_schema_fails():
    assert run("F15", context(homepage_html=doc("<p>hi</p>"))).status == FAIL


def test_f15_incomplete_schema_fails():
    res = run("F15", context(homepage_html=doc("", head=ld(
        '{"@type":"LocalBusiness","name":"Peak Roofing"}'
    ))))
    assert res.status == FAIL
    assert "missing" in res.note


def test_f15_wrong_phone_in_schema_fails_and_says_so():
    res = run("F15", context(homepage_html=doc("", head=ld(
        '{"@type":"LocalBusiness","name":"Peak","address":{"streetAddress":"x"},'
        '"telephone":"(844) 555-9900"}'
    ))))
    assert res.status == FAIL
    assert "844" in res.note


# ── CHOSEN ────────────────────────────────────────────────────────────────────


def test_c1_https_passes():
    assert run("C1", context(homepage_html=doc("<p>hi</p>"))).status == PASS


def test_c1_unreadable_site_fails():
    crawl = SiteCrawl(base_url=HOST, homepage=FetchResult(url=HOST, status=500))
    ctx = AuditContext(place=PLACE, site=build(crawl), market=COLORADO_SPRINGS, now=NOW)
    assert run("C1", ctx).status == FAIL


def test_c6_header_tel_passes():
    res = run("C6", context(homepage_html=doc('<header><a href="tel:+17195550142">Call</a></header>')))
    assert res.status == PASS
    assert "top of the page" in res.note


def test_c6_tel_outside_the_header_still_passes_but_says_so():
    res = run("C6", context(homepage_html=doc('<footer><a href="tel:+17195550142">Call</a></footer>')))
    assert res.status == PASS
    assert "not from the page header" in res.note


def test_c6_plain_text_number_fails():
    assert run("C6", context(homepage_html=doc("<p>(719) 555-0142</p>"))).status == FAIL


SHORT_FORM = ('<form action="/send" method="post">'
              '<input name="name"><input name="email" type="email"><input name="phone">'
              '<input type="hidden" name="_csrf"><input type="submit"></form>')
LONG_FORM = ('<form action="/send" method="post">' +
             "".join(f'<input name="f{i}">' for i in range(4)) +
             '<input name="email"><input name="phone"><textarea name="message"></textarea></form>')


def test_c8_short_form_passes():
    res = run("C8", context(homepage_html=doc(SHORT_FORM)))
    assert res.status == PASS
    assert res.observed["field_count"] == 3, "hidden and submit do not count as friction"


def test_c8_long_form_fails():
    res = run("C8", context(homepage_html=doc(LONG_FORM)))
    assert res.status == FAIL
    assert res.observed["field_count"] == 7


def test_c8_ignores_the_search_box():
    res = run("C8", context(homepage_html=doc(
        '<form action="/search"><input name="s" type="search"><input type="submit"></form>' + SHORT_FORM
    )))
    assert res.status == PASS
    assert res.observed["field_count"] == 3


def test_c8_no_form_at_all_fails():
    assert run("C8", context(homepage_html=doc("<p>Call us</p>"))).status == FAIL


def test_c8_finds_the_form_on_the_contact_page():
    res = run("C8", context({"/contact/": doc(SHORT_FORM)}, homepage_html=doc("<p>hi</p>")))
    assert res.status == PASS
    assert res.observed["form_page"] == "/contact/"


@pytest.mark.parametrize(
    "body, status",
    [
        ("<h2>What our customers say</h2>", PASS),
        ('<div class="birdeye-widget"></div>', PASS),
        ("<p>We install roofs.</p>", FAIL),
    ],
)
def test_c10_reviews_on_homepage(body, status):
    assert run("C10", context(homepage_html=doc(body))).status == status


def test_c10_review_schema_on_the_homepage_counts():
    res = run("C10", context(homepage_html=doc("", head=ld('{"@type":"Review","name":"x"}'))))
    assert res.status == PASS


@pytest.mark.parametrize(
    "code, passing, failing",
    [
        ("C11", "<p>Licensed and insured in Colorado.</p>", "<p>We roof things.</p>"),
        ("C13", "<p>10 year workmanship warranty.</p>", "<p>We roof things.</p>"),
        ("C14", "<p>Financing available.</p>", "<p>We roof things.</p>"),
    ],
)
def test_simple_text_checks(code, passing, failing):
    assert run(code, context(homepage_html=doc(passing))).status == PASS
    assert run(code, context(homepage_html=doc(failing))).status == FAIL


def test_c12_tier_passes():
    res = run("C12", context(homepage_html=doc("<p>GAF Master Elite contractor.</p>")))
    assert res.status == PASS


def test_c12_brand_without_a_tier_fails_and_names_the_brand():
    res = run("C12", context(homepage_html=doc("<p>We install GAF shingles.</p>")))
    assert res.status == FAIL
    assert res.observed["manufacturer"] == "gaf"


def test_c14_detects_a_financing_vendor_script():
    res = run("C14", context(homepage_html=doc('<script src="https://widget.wisetack.com/x.js"></script>')))
    assert res.status == PASS


def test_c15_storm_page_passes():
    res = run("C15", context(sitemap=("/insurance-claims/",)))
    assert res.status == PASS


def test_c15_without_a_storm_page_fails():
    assert run("C15", context(sitemap=("/about/",))).status == FAIL


@pytest.mark.parametrize("year, status", [(2026, PASS), (2025, PASS), (2021, FAIL)])
def test_c16_footer_copyright(year, status):
    assert run("C16", context(homepage_html=doc(f"<footer>© {year} Peak Roofing</footer>"))).status == status


def test_c16_without_a_copyright_skips():
    assert run("C16", context(homepage_html=doc("<footer>Peak Roofing</footer>"))).status == SKIPPED


# ── Measurement layer ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code, markup",
    [
        ("M1", '<script src="https://www.googletagmanager.com/gtag/js?id=G-ABC"></script>'),
        ("M2", "<script>gtag('config', 'AW-12345');</script>"),
        ("M4", '<script src="//cdn.callrail.com/companies/1/x/swap.js"></script>'),
    ],
)
def test_measurement_detects_its_tag(code, markup):
    assert run(code, context(homepage_html=doc(markup))).status == PASS
    assert run(code, context(homepage_html=doc("<p>nothing</p>"))).status == FAIL


def test_m3_without_an_ad_library_answer_records_but_does_not_judge():
    """A missing pixel on a business not running ads is not a finding."""
    res = run("M3", context(homepage_html=doc("<p>nothing</p>")))
    assert res.status == SKIPPED
    assert res.observed["ads_status"] == "unknown"


def test_m3_running_ads_without_a_pixel_is_the_finding():
    ctx = context(homepage_html=doc("<p>nothing</p>"))
    ctx.ads = {"active": True}
    res = run("M3", ctx)
    assert res.status == FAIL
    assert "no tracking pixel" in res.note


def test_m3_not_running_ads_is_not_a_finding():
    ctx = context(homepage_html=doc("<p>nothing</p>"))
    ctx.ads = {"active": False}
    assert run("M3", ctx).status == SKIPPED


# ── Preconditions shared by every crawl check ─────────────────────────────────


def test_robots_disallow_skips_rather_than_fails():
    """Guardrail 6. Disallowed means skipped and noted, never counted against him."""
    crawl = SiteCrawl(
        base_url=HOST,
        homepage=FetchResult(url=HOST, blocked_by_robots=True),
        robots_blocked=[HOST],
    )
    ctx = AuditContext(place=PLACE, site=build(crawl), market=COLORADO_SPRINGS, now=NOW)
    for code in ("C6", "C8", "C10", "C11", "F10", "F14", "F15", "M1"):
        res = run(code, ctx)
        assert res.status == SKIPPED, code
        assert "disallows crawling" in res.note, code


def test_an_unreachable_site_skips_crawl_checks_but_still_scores_places_checks():
    crawl = SiteCrawl(base_url=HOST, homepage=FetchResult(url=HOST, status=500))
    ctx = AuditContext(place=PLACE, site=build(crawl), market=COLORADO_SPRINGS, now=NOW)
    assert run("C6", ctx).status == SKIPPED
    assert run("F3", ctx).status == PASS, "Places checks do not need the site"


# ── The runner ────────────────────────────────────────────────────────────────


def test_run_checks_marks_unimplemented_definitions_skipped():
    from app.checks.definitions import CHECK_DEFINITIONS

    results = run_checks(context(homepage_html=doc("<p>hi</p>")), CHECK_DEFINITIONS)
    assert "F1" not in results, "disabled definitions never run"
    assert results["F3"].status == PASS

    # As of Friday every enabled definition has an implementation. If this
    # fails, a definition was enabled without code behind it and every audit
    # is silently scoring it as skipped.
    from app.checks.base import REGISTRY

    enabled = {r["code"] for r in CHECK_DEFINITIONS if r["enabled"]}
    unimplemented = enabled - set(REGISTRY)
    assert not unimplemented, f"enabled with no implementation: {sorted(unimplemented)}"


def test_render_checks_skip_when_there_is_no_render():
    """A renderer that was down is not a defect in his website."""
    from app.checks.definitions import CHECK_DEFINITIONS

    results = run_checks(context(homepage_html=doc("<p>hi</p>")), CHECK_DEFINITIONS)
    for code in ("C2", "C5", "C7"):
        assert results[code].status == SKIPPED, code
        assert "not rendered" in results[code].note, code


def test_a_raising_check_is_recorded_as_error_and_does_not_stop_the_rest():
    from app.checks.base import REGISTRY as reg

    original = reg["F3"]

    def explode(ctx):
        raise RuntimeError("boom")

    reg["F3"] = explode
    try:
        results = run_checks(context(homepage_html=doc("<p>hi</p>")),
                             [{"code": "F3", "enabled": True}, {"code": "F4", "enabled": True}])
    finally:
        reg["F3"] = original

    assert results["F3"].status == "error"
    assert "RuntimeError" in results["F3"].observed["error"]
    assert results["F4"].status == PASS, "the rest of the audit continues"


def test_statuses_shape_feeds_scoring():
    from app.checks.definitions import CHECK_DEFINITIONS
    from app.scoring import compute, outcomes_from

    results = run_checks(context(homepage_html=doc("<p>hi</p>")), CHECK_DEFINITIONS)
    score = compute(outcomes_from(statuses(results), CHECK_DEFINITIONS))
    assert 0 <= score.total <= 100
    assert score.partial, "Chosen and Booked are mostly unimplemented this week"


def test_every_registered_check_has_a_definition():
    from app.checks.definitions import by_code

    known = by_code()
    for code in REGISTRY:
        assert code in known, f"{code} is implemented with no definition"


def test_check_results_serialize_without_nulls():
    res = run("F3", context())
    row = res.to_dict()
    assert row["code"] == "F3" and row["status"] == PASS
    assert all(v is not None for v in row.get("observed", {}).values())


def test_importing_the_package_registers_every_check():
    """Regression: the registry is built by decorator, so a module nobody
    imports makes every one of its checks report 'not implemented' at runtime
    while the tests still pass, because the tests import it directly."""
    import subprocess
    import sys

    probe = (
        "import app.pipeline;"
        "from app.checks.base import REGISTRY;"
        "print(len(REGISTRY))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert int(out.stdout.strip()) >= 23, "app.pipeline must pull in the check implementations"


def test_c8_counts_a_checkbox_group_as_one_question():
    """Regression, found on a real Elementor form.

    A four option checkbox group renders four inputs sharing one name, and a
    form builder writes that name as `form_fields[x][]`. Counting inputs turned
    one question into four and reported 16 questions on a 13 question form. The
    number lands in front of a contractor, so it has to be the number he would
    count himself.
    """
    checkboxes = "".join(
        f'<input type="checkbox" name="form_fields[roof_type][]" value="{v}">'
        for v in ("asphalt", "tile", "metal", "flat")
    )
    html = doc(
        '<form action="/send" method="post">'
        '<input name="form_fields[name]"><input name="form_fields[email]" type="email">'
        f"{checkboxes}</form>"
    )
    res = run("C8", context(homepage_html=html))
    assert res.observed["field_count"] == 3, "name, email, and one roof type question"
    assert res.status == PASS


def test_c8_counts_a_radio_group_as_one_question():
    radios = "".join(f'<input type="radio" name="urgency" value="{v}">' for v in ("now", "month"))
    res = run("C8", context(homepage_html=doc(
        f'<form action="/s" method="post"><input name="email"><input name="phone">{radios}</form>'
    )))
    assert res.observed["field_count"] == 3


def test_c15_does_not_count_a_page_aimed_at_insurance_agents():
    """Regression, found on a real site.

    /partners/insurance-agents/ recruits adjusters. It is not a page that helps
    a homeowner file a hail claim, and scoring it as one would have put a false
    finding in front of a contractor.
    """
    res = run("C15", context(sitemap=("/partners/insurance-agents/", "/about-us/")))
    assert res.status == FAIL


@pytest.mark.parametrize(
    "path, status",
    [
        ("/storm-damage/", PASS),
        ("/hail-damage-repair/", PASS),
        ("/insurance-claims/", PASS),
        ("/roof-claim-help/", PASS),
        ("/partners/insurance-agents/", FAIL),
        ("/careers/insurance-claims-adjuster/", FAIL),
        ("/commercial-storm-response/", FAIL),
    ],
)
def test_c15_wants_a_homeowner_facing_claim_page(path, status):
    assert run("C15", context(sitemap=(path,))).status == status


# ── Blog contamination, all found on real sites once the sitemap was read ────


@pytest.mark.parametrize(
    "path, status, why",
    [
        ("/storm-damage/", PASS, "dedicated service page"),
        ("/insurance-claims/", PASS, "dedicated service page"),
        ("/hail-damage-repair/", PASS, "dedicated service page"),
        ("/hail-proof-shingles/", FAIL, "a product page, not claim help"),
        ("/5-critical-hail-damage-warning-signs-before-the-next-storm/", FAIL, "an article"),
        ("/colorado-hailstorm-activity-update-front-range-storm-conditions/", FAIL, "an article"),
        ("/acv-vs-rcv-roof-coverage-the-hidden-reason-claims-dont-pay-in-full/", FAIL, "an article"),
        ("/blog/insurance-claims/", FAIL, "under a blog"),
        ("/2026/03/storm-damage/", FAIL, "dated archive"),
    ],
)
def test_c15_separates_a_claim_page_from_writing_about_hail(path, status, why):
    assert run("C15", context(sitemap=(path,))).status == status, why


@pytest.mark.parametrize(
    "paths, count, why",
    [
        (("/roofing-monument/", "/roofing-fountain/", "/roofing-falcon/"), 3, "service area pages"),
        (("/colorado-springs-co/",), 1, "city page with a short slug"),
        (("/locations/locations/broadmoor/",), 1, "nested service area page"),
        (("/wui-roof-code-in-colorado-springs/",), 0, "an article naming the city"),
        (("/snow-roofing-materials-for-colorado-springs/",), 0, "an article naming the city"),
        (("/colorado-springs-roof-snow-load/",), 0, "an article naming the city"),
    ],
)
def test_f10_does_not_let_the_blog_carry_service_area_coverage(paths, count, why):
    res = run("F10", context(sitemap=paths))
    assert res.observed["city_page_count"] == count, why


def test_c8_evidence_lists_questions_not_raw_inputs():
    checkboxes = "".join(
        f'<input type="checkbox" name="roof[]" value="{v}">' for v in ("a", "b", "c")
    )
    res = run("C8", context(homepage_html=doc(
        f'<form action="/s" method="post"><input name="email"><input name="phone">{checkboxes}</form>'
    )))
    assert res.observed["fields"] == ["email", "phone", "roof"]


# ── URL normalization ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://x.com/?utm_source=google&utm_medium=organic", "https://x.com/"),
        ("https://x.com/roof?gclid=abc", "https://x.com/roof"),
        ("https://x.com/roof?fbclid=abc&msclkid=d", "https://x.com/roof"),
        ("https://x.com/blog?page=2", "https://x.com/blog?page=2"),
        ("https://x.com/blog?page=2&utm_campaign=spring", "https://x.com/blog?page=2"),
        ("https://X.com/A?UTM_SOURCE=g", "https://x.com/A"),
        ("https://x.com/a#section", "https://x.com/a"),
    ],
)
def test_normalize_url_drops_tracking_but_keeps_meaning(raw, expected):
    """Places returns the website with Google's own campaign tags attached.

    Carrying them re-spends provider quota on the same page, shows a contractor
    a tracked link in his own report, and stops CrUX field data from matching.
    Dropping ?page=2 would instead change which page we audited.
    """
    from app.tools.crawl import normalize_url

    assert normalize_url(raw) == expected


def test_c8_recognises_a_gravity_forms_lead_form():
    """Regression, found on a real site. Gravity Forms names its inputs
    input_9, input_1.3: nothing contact-ish in any name, but the email field
    still carries type=email. The name-only heuristic skipped B2 and C8 on 17
    of 40 prospects in the first full batch."""
    gravity = ('<form action="/roof-replacement/" method="post">'
               '<input name="input_9" type="text"><input name="input_1.3" type="text">'
               '<input name="input_2" type="email"><input name="input_3" type="tel">'
               '<textarea name="input_8"></textarea></form>')
    res = run("C8", context(homepage_html=doc(gravity)))
    assert res.status == PASS
    assert res.observed["field_count"] == 5


def test_the_search_box_is_still_not_a_lead_form():
    res = run("C8", context(homepage_html=doc(
        '<form action="/search"><input name="s" type="search"></form>'
    )))
    assert res.status == FAIL, "no lead form found is a fail, the search box does not count"


def test_locality_queries_cover_the_enumerated_metro():
    from app.markets import COLORADO_SPRINGS, resolve_market
    from app.tools.places import queries_for_market

    queries = queries_for_market(COLORADO_SPRINGS)
    assert "roofing contractor in Colorado Springs, CO" in queries
    assert "roofing contractor in Monument, CO" in queries
    assert "roofing contractor in Fountain, CO" in queries
    # the anchor city is not repeated as a locality query
    assert sum("Colorado Springs, CO" in q for q in queries) >= 1
    assert len(queries) > 20

    # an unmapped market gets the metro variants alone
    assert len(queries_for_market(resolve_market("Boise"))) == 5
