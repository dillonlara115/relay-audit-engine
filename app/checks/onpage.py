"""The checks that need nothing but a crawl and the Places record.

Every check here is a comparison over facts already extracted. Notes are written
as plain sentences because they are what a human reads when picking the three
report findings, but they are internal: they may name a mechanism. The copy that
reaches a contractor is written by the diagnostician, under the brand rules.

Where a data source cannot answer a question, the check skips and says so. It
never guesses, and it never reports a limitation as a failure.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Iterable

from app.checks.base import AuditContext, CheckResult, check, result, skip
from app.checks.extract import jsonld_types
from app.tools.phones import parse_phone, same_number

# ── Thresholds, criteria doc section 1 and 2 ──────────────────────────────────

REVIEW_COUNT_FULL = 50
RATING_FULL = 4.5
REVIEW_RECENCY_DAYS = 30
CONTENT_FRESHNESS_DAYS = 180
CITY_PAGES_FULL = 3
MAX_FORM_FIELDS = 5

ROOFING_CATEGORY = "roofing_contractor"

# ── Vocabulary ────────────────────────────────────────────────────────────────

LICENSE_TERMS = ("licensed and insured", "licensed & insured", "licensed, bonded",
                 "bonded and insured", "fully licensed", "fully insured", "license #",
                 "license number", "lic #", "insured and licensed")

WARRANTY_TERMS = ("workmanship warranty", "labor warranty", "labour warranty",
                  "warranty on workmanship", "year workmanship", "lifetime warranty",
                  "warranty on labor", "guaranteed workmanship", "workmanship guarantee")

FINANCING_TERMS = ("financing", "finance your", "payment plan", "monthly payments",
                   "0% interest", "no interest", "apply for financing", "flexible payment")
FINANCING_VENDORS = ("greensky", "hearth.com", "wisetack", "acornfinance", "servicefinance",
                     "foundation finance", "synchrony", "enhancify")

MANUFACTURER_TIERS = ("master elite", "gaf master", "preferred contractor", "platinum preferred",
                      "select shinglemaster", "shinglemaster", "certified contractor",
                      "owens corning preferred", "certainteed select", "certified installer")
MANUFACTURERS = ("gaf", "owens corning", "certainteed", "malarkey", "iko", "tamko", "atlas roofing")

TESTIMONIAL_TERMS = ("what our customers say", "what our clients say", "customer testimonial",
                     "testimonials", "client testimonial", "what people are saying",
                     "hear from our customers", "customer reviews", "read our reviews",
                     "verified review", "5-star review", "five star review")
REVIEW_WIDGETS = ("birdeye", "podium.com", "nicejob", "trustindex", "elfsight", "reviewsonmywebsite",
                  "shapo.io", "grade.us", "sociablekit", "reputationstacker", "widget.reviewshake")

# C15 asks for a dedicated claim-help page, so the fragments name claim intent
# rather than the weather. A bare "hail" matches /hail-proof-shingles/, which is
# a product page, and a bare "insurance" matches /partners/insurance-agents/,
# which recruits adjusters.
STORM_PATH_FRAGMENTS = ("insurance-claim", "insurance-restoration", "storm-damage",
                        "hail-damage", "storm-restoration", "claim-help", "file-a-claim",
                        "roof-claim", "claims")

# Once a sitemap is read we see the whole blog, and a roofer who writes about
# hail every spring is not the same as a roofer with a claim-help page. A
# dedicated service page has a short slug; an article does not.
MAX_SERVICE_PAGE_SLUG_WORDS = 4

# Pages aimed at other businesses, not at a homeowner with a leaking roof.
B2B_PATH_FRAGMENTS = ("partner", "agent", "property-manager", "real-estate", "realtor",
                      "vendor", "career", "job", "affiliate", "wholesale", "commercial")
SERVICE_AREA_FRAGMENTS = ("service-area", "areas-we-serve", "areas-served", "locations",
                          "service-areas", "cities-we-serve", "where-we-work")

# Measurement layer fingerprints.
GA4_FINGERPRINTS = ("googletagmanager.com/gtag/js", "gtag('config', 'g-", 'gtag("config", "g-',
                    "google-analytics.com/g/collect", "googletagmanager.com/gtm.js")
ADS_CONVERSION_FINGERPRINTS = ("gtag('config', 'aw-", 'gtag("config", "aw-', "google_conversion_id",
                               "googleadservices.com/pagead/conversion", "aw-conversion")
META_PIXEL_FINGERPRINTS = ("connect.facebook.net/en_us/fbevents.js", "fbq('init'", 'fbq("init"',
                           "facebook.com/tr?id=")
CALL_TRACKING_FINGERPRINTS = ("callrail.com", "cdn.callrail", "calltrk.com", "calltrackingmetrics",
                              "tctm.co", "invoca.net", "whatconverts.com", "marchex.io",
                              "dialogtech", "retreaver", "callfire", "phonewagon")

LOCAL_BUSINESS_TYPES = frozenset({
    "localbusiness", "roofingcontractor", "homeandconstructionbusiness", "generalcontractor",
    "professionalservice", "contractor", "hvacbusiness",
})
REVIEW_SCHEMA_TYPES = frozenset({"review", "aggregaterating"})

_COPYRIGHT_YEAR = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
_SEARCH_FIELD_HINTS = ("email", "phone", "tel", "name", "message", "zip", "address")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _slug_words(path: str) -> int:
    """Word count of a path's last real segment. Articles run long, pages do not."""
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        return 0
    return len([w for w in re.split(r"[-_]+", segments[-1]) if w])


def _is_article(path: str, *, limit: int = MAX_SERVICE_PAGE_SLUG_WORDS) -> bool:
    lowered = path.lower()
    if "/blog/" in lowered or "/news/" in lowered or re.search(r"/20\d{2}/", lowered):
        return True
    return _slug_words(path) > limit


def _any_in(haystack: str, needles: Iterable[str]) -> str | None:
    for needle in needles:
        if needle in haystack:
            return needle
    return None


def _unreachable(ctx: AuditContext, code: str) -> CheckResult | None:
    """Shared precondition. A site we could not read is skipped, not failed."""
    if ctx.site.robots_blocked and not ctx.site.pages:
        return skip(code, "The site disallows crawling, so this was not checked.",
                    robots_blocked=list(ctx.site.robots_blocked))
    if not ctx.site.reachable or not ctx.site.pages:
        return skip(code, "No page could be read from the site.")
    return None


# ── FOUND ─────────────────────────────────────────────────────────────────────


@check("F2")
def f2_primary_category(ctx: AuditContext) -> CheckResult:
    primary = ctx.field("primary_type")
    if not primary:
        return skip("F2", "Google did not return a primary category.")
    ok = primary == ROOFING_CATEGORY
    readable = primary.replace("_", " ")
    return result(
        "F2",
        ok,
        f"Google lists the primary category as {readable}."
        if ok
        else f"Google lists the primary category as {readable}, not roofing contractor.",
        primary_type=primary,
    )


@check("F3")
def f3_review_count(ctx: AuditContext) -> CheckResult:
    count = ctx.field("review_count")
    if count is None:
        return skip("F3", "Google did not return a review count.")
    return result(
        "F3", count >= REVIEW_COUNT_FULL,
        f"{count} Google reviews." if count >= REVIEW_COUNT_FULL
        else f"{count} Google reviews, under the {REVIEW_COUNT_FULL} mark.",
        review_count=count,
    )


@check("F4")
def f4_rating(ctx: AuditContext) -> CheckResult:
    rating = ctx.field("rating")
    if rating is None:
        return skip("F4", "Google did not return a rating.")
    return result(
        "F4", rating >= RATING_FULL,
        f"Rated {rating:.1f} on Google." if rating >= RATING_FULL
        else f"Rated {rating:.1f} on Google, under {RATING_FULL}.",
        rating=rating,
    )


@check("F5")
def f5_review_recency(ctx: AuditContext) -> CheckResult:
    """Newest review within 30 days.

    Places returns at most five reviews and does not guarantee they are the
    newest, so the newest one we saw is a lower bound. Inside the window that is
    conclusive. Outside it, with a truncated sample, it is not, and the check
    skips rather than telling a roofer his reviews have dried up when they have
    not. Same reasoning as the gate's storm chaser check.
    """
    latest = ctx.field("latest_review_at")
    if latest is None:
        return skip("F5", "Google returned no review timestamps.")

    age = (ctx.now - latest).days
    if age <= REVIEW_RECENCY_DAYS:
        return result("F5", True, f"Newest review we can see is {age} days old.",
                      latest_review_at=latest, age_days=age)

    sample = ctx.field("review_sample_size")
    total = ctx.field("review_count")
    truncated = sample is not None and total is not None and total > sample
    if truncated:
        return skip(
            "F5",
            f"Newest of the {sample} reviews Google returned is {age} days old, "
            f"but that is a sample of {total} and not necessarily the newest.",
            latest_review_at=latest, age_days=age, review_sample_size=sample, review_count=total,
        )
    return result("F5", False, f"Newest review is {age} days old.",
                  latest_review_at=latest, age_days=age)


@check("F7")
def f7_phone_match(ctx: AuditContext) -> CheckResult:
    """The invisible gap. Roughly a third of local businesses fail it."""
    gbp_raw = ctx.field("gbp_phone")
    gbp = parse_phone(gbp_raw)
    if gbp is None:
        return skip("F7", "No usable phone number on the Google profile.")

    blocked = _unreachable(ctx, "F7")
    if blocked:
        return blocked

    site_phones = ctx.site.phones()
    if not site_phones:
        return result("F7", False, "No clickable phone number found on the site to compare.",
                      gbp_phone=gbp.e164)

    matched = [p for p in site_phones if same_number(p.e164, gbp.e164)]
    primary = site_phones[0]
    if matched:
        note = f"The site and the Google profile both show {gbp.national}."
        if primary.e164 != gbp.e164:
            note = (f"The site shows {gbp.national} as well as {primary.national}, "
                    f"and the Google profile shows {gbp.national}.")
        return result("F7", True, note, gbp_phone=gbp.e164,
                      site_phones=[p.e164 for p in site_phones])

    return result(
        "F7", False,
        f"The site shows {primary.national} but the Google profile shows {gbp.national}.",
        gbp_phone=gbp.e164, site_phones=[p.e164 for p in site_phones],
        primary_site_phone=primary.e164,
    )


@check("F10")
def f10_service_area(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "F10")
    if blocked:
        return blocked

    known_cities = set()
    if ctx.market is not None:
        known_cities = {_slug(c) for c in ctx.market.cities if len(c) > 3}

    hits: dict[str, str] = {}
    for path in ctx.site.all_paths:
        lowered = path.lower()
        if any(fragment in lowered for fragment in SERVICE_AREA_FRAGMENTS) and lowered.count("/") > 2:
            hits[path] = "service area page"
            continue
        for city in known_cities:
            if city not in lowered:
                continue
            # "Roofing in Monument" is a service area page. "WUI roof code in
            # Colorado Springs" is an article that happens to name the city, and
            # counting it would let a blog carry a check about coverage.
            allowance = len([w for w in city.split("-") if w]) + 2
            if not _is_article(path, limit=allowance):
                hits[path] = city.replace("-", " ")
            break

    count = len(hits)
    ok = count >= CITY_PAGES_FULL
    return result(
        "F10", ok,
        f"{count} city or service area pages published." if ok
        else f"{count} city or service area pages published, under {CITY_PAGES_FULL}.",
        city_pages=sorted(hits), city_page_count=count,
    )


@check("F11")
def f11_content_freshness(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "F11")
    if blocked:
        return blocked

    newest = ctx.site.newest_date()
    if newest is None:
        return skip("F11", "No page on the site publishes a date we can read.")

    age = (ctx.now - newest).days
    ok = age <= CONTENT_FRESHNESS_DAYS
    return result(
        "F11", ok,
        f"Most recent dated page is {age} days old." if ok
        else f"Most recent dated page is {age} days old, over {CONTENT_FRESHNESS_DAYS}.",
        newest_content_at=newest, age_days=age,
    )


@check("F14")
def f14_review_schema(ctx: AuditContext) -> CheckResult:
    """Close to nine in ten carry none of their reviews in machine-readable form."""
    blocked = _unreachable(ctx, "F14")
    if blocked:
        return blocked

    found = sorted({
        t for block in ctx.site.jsonld for t in jsonld_types(block) if t in REVIEW_SCHEMA_TYPES
    })
    if not found:
        # Microdata is rarer but still valid and still machine readable.
        if "itemtype" in ctx.site.html and _any_in(ctx.site.html, ("schema.org/review", "schema.org/aggregaterating")):
            return result("F14", True, "Reviews are published in a machine-readable form.",
                          schema_format="microdata")
    return result(
        "F14", bool(found),
        "Reviews are published in a machine-readable form." if found
        else "No reviews are published on the site in a machine-readable form.",
        schema_types=found or None,
    )


@check("F15")
def f15_business_schema(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "F15")
    if blocked:
        return blocked

    candidates = [b for b in ctx.site.jsonld if jsonld_types(b) & LOCAL_BUSINESS_TYPES]
    if not candidates:
        return result("F15", False, "The site publishes no business listing a search engine can read.")

    gbp = parse_phone(ctx.field("gbp_phone"))
    for block in candidates:
        name = block.get("name")
        address = block.get("address")
        telephone = block.get("telephone")
        if not (name and address and telephone):
            continue
        if gbp and not same_number(str(telephone), gbp.e164):
            return result(
                "F15", False,
                f"The site's business listing shows {telephone}, "
                f"which is not the number on the Google profile.",
                schema_name=str(name), schema_phone=str(telephone), gbp_phone=gbp.e164,
            )
        return result("F15", True, "The site publishes a complete business listing.",
                      schema_name=str(name), schema_phone=str(telephone))

    return result("F15", False,
                  "The site's business listing is missing a name, address, or phone number.")


# ── CHOSEN ────────────────────────────────────────────────────────────────────


@check("C1")
def c1_site_loads(ctx: AuditContext) -> CheckResult:
    page = ctx.site.homepage
    if page is None:
        if ctx.site.robots_blocked:
            return skip("C1", "The site disallows crawling, so this was not checked.")
        return result("C1", False, "The site did not return a readable page.")
    secure = page.url.startswith("https://")
    return result(
        "C1", secure,
        "The site loads over a secure connection." if secure
        else "The site does not load over a secure connection.",
        final_url=page.url,
    )


@check("C6")
def c6_click_to_call(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "C6")
    if blocked:
        return blocked

    page = ctx.site.homepage
    header = list(page.header_tel_hrefs) if page else []
    anywhere = list(page.tel_hrefs) if page else []

    if header:
        return result("C6", True, "The phone number at the top of the page can be tapped to call.",
                      header_tel=header[:3])
    if anywhere:
        return result("C6", True,
                      "The phone number can be tapped to call, though not from the page header.",
                      tel_hrefs=anywhere[:3])
    return result("C6", False,
                  "The phone number on the homepage cannot be tapped to call on a phone.")


def _primary_form(ctx: AuditContext):
    """The form a homeowner would actually fill in.

    Search boxes and newsletter signups are excluded, and a form with no way to
    reach the person back is not a lead form.
    """
    ordered = []
    if ctx.site.homepage is not None:
        ordered.append(ctx.site.homepage)
    ordered.extend(p for p in ctx.site.pages if "contact" in p.path.lower())
    ordered.extend(p for p in ctx.site.pages if p not in ordered)

    for page in ordered:
        for form in page.forms:
            if form.looks_like_search or form.field_count < 2:
                continue
            names = " ".join(f.name.lower() for f in form.visible_fields)
            if any(hint in names for hint in _SEARCH_FIELD_HINTS):
                return form, page
    return None, None


@check("C8")
def c8_form_friction(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "C8")
    if blocked:
        return blocked

    form, page = _primary_form(ctx)
    if form is None:
        return result("C8", False, "No contact form was found on the site.")

    count = form.field_count
    ok = count <= MAX_FORM_FIELDS
    return result(
        "C8", ok,
        f"The contact form asks for {count} pieces of information." if ok
        else f"The contact form asks for {count} pieces of information, "
             f"more than the {MAX_FORM_FIELDS} that keeps people filling it in.",
        field_count=count,
        fields=[q for q in form.questions if not q.startswith("__unnamed_")][:12],
        form_page=page.path if page else None,
    )


@check("C10")
def c10_reviews_on_page(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "C10")
    if blocked:
        return blocked

    page = ctx.site.homepage
    if page is None:
        return skip("C10", "No homepage could be read.")

    schema = bool({t for block in page.jsonld for t in jsonld_types(block)} & REVIEW_SCHEMA_TYPES)
    phrase = _any_in(page.text, TESTIMONIAL_TERMS)
    widget = _any_in(page.html, REVIEW_WIDGETS)

    ok = bool(schema or phrase or widget)
    evidence = "review markup" if schema else (f"the phrase {phrase!r}" if phrase else
                                               (f"a {widget} review widget" if widget else None))
    return result(
        "C10", ok,
        f"Customer reviews appear on the homepage, shown by {evidence}." if ok
        else "No customer reviews appear on the homepage.",
        schema=schema or None, phrase=phrase, widget=widget,
    )


def _text_check(code: str, ctx: AuditContext, terms: Iterable[str], yes: str, no: str,
                extra_terms: Iterable[str] = ()) -> CheckResult:
    blocked = _unreachable(ctx, code)
    if blocked:
        return blocked
    hit = _any_in(ctx.site.text, terms) or _any_in(ctx.site.html, extra_terms)
    return result(code, bool(hit), yes if hit else no, matched=hit)


@check("C11")
def c11_licensed_insured(ctx: AuditContext) -> CheckResult:
    return _text_check("C11", ctx, LICENSE_TERMS,
                       "The site states the business is licensed and insured.",
                       "The site never states the business is licensed and insured.")


@check("C12")
def c12_manufacturer_credential(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "C12")
    if blocked:
        return blocked
    tier = _any_in(ctx.site.text, MANUFACTURER_TIERS)
    brand = _any_in(ctx.site.text, MANUFACTURERS)
    ok = bool(tier)
    if not ok and brand:
        return result("C12", False,
                      f"The site mentions {brand} but claims no certification level.",
                      manufacturer=brand)
    return result(
        "C12", ok,
        f"The site claims a manufacturer certification: {tier}." if ok
        else "The site claims no manufacturer certification.",
        tier=tier, manufacturer=brand,
    )


@check("C13")
def c13_warranty(ctx: AuditContext) -> CheckResult:
    return _text_check("C13", ctx, WARRANTY_TERMS,
                       "The site states a workmanship warranty.",
                       "The site states no workmanship warranty.")


@check("C14")
def c14_financing(ctx: AuditContext) -> CheckResult:
    return _text_check("C14", ctx, FINANCING_TERMS,
                       "The site offers a way to pay over time.",
                       "The site offers no way to pay over time.",
                       extra_terms=FINANCING_VENDORS)


@check("C15")
def c15_storm_page(ctx: AuditContext) -> CheckResult:
    """Front Range weight. Hail claims are the buying trigger here."""
    blocked = _unreachable(ctx, "C15")
    if blocked:
        return blocked

    pages = [
        p for p in ctx.site.all_paths
        if p.strip("/")
        and any(f in p.lower() for f in STORM_PATH_FRAGMENTS)
        and not any(b in p.lower() for b in B2B_PATH_FRAGMENTS)
        and not _is_article(p)
    ]
    ok = bool(pages)
    return result(
        "C15", ok,
        f"The site has a page for storm damage and insurance claims: {pages[0]}." if ok
        else "The site has no page for storm damage or insurance claims.",
        storm_pages=sorted(pages)[:5] or None,
    )


@check("C16")
def c16_footer_copyright(ctx: AuditContext) -> CheckResult:
    blocked = _unreachable(ctx, "C16")
    if blocked:
        return blocked

    years = [int(m.group(1)) for m in _COPYRIGHT_YEAR.finditer(ctx.site.text)]
    plausible = [y for y in years if 1990 <= y <= ctx.now.year + 1]
    if not plausible:
        return skip("C16", "The site shows no copyright year.")

    newest = max(plausible)
    ok = newest >= ctx.now.year - 1
    return result(
        "C16", ok,
        f"The footer shows {newest}." if ok
        else f"The footer still shows {newest}, which makes the site look abandoned.",
        copyright_year=newest,
    )


# ── Measurement layer, 0 points, always recorded ──────────────────────────────


def _fingerprint_check(code: str, ctx: AuditContext, fingerprints: Iterable[str],
                       yes: str, no: str) -> CheckResult:
    blocked = _unreachable(ctx, code)
    if blocked:
        return blocked
    hit = _any_in(ctx.site.html, fingerprints)
    return result(code, bool(hit), yes if hit else no, matched=hit)


@check("M1")
def m1_ga4(ctx: AuditContext) -> CheckResult:
    return _fingerprint_check("M1", ctx, GA4_FINGERPRINTS,
                              "Website analytics are installed.",
                              "No website analytics are installed.")


@check("M2")
def m2_ads_conversion(ctx: AuditContext) -> CheckResult:
    return _fingerprint_check("M2", ctx, ADS_CONVERSION_FINGERPRINTS,
                              "Google Ads conversion tracking is installed.",
                              "No Google Ads conversion tracking is installed.")


@check("M3")
def m3_meta_pixel(ctx: AuditContext) -> CheckResult:
    """Only a finding when active Meta ads are confirmed.

    A missing pixel on a business not running ads is not a finding, so without
    an Ad Library answer this records presence and says the comparison was not
    available.
    """
    blocked = _unreachable(ctx, "M3")
    if blocked:
        return blocked

    hit = _any_in(ctx.site.html, META_PIXEL_FINGERPRINTS)
    running_ads = (ctx.ads or {}).get("active") if ctx.ads else None

    if running_ads is None:
        return skip(
            "M3",
            "A tracking pixel is installed." if hit else "No tracking pixel is installed.",
            pixel_present=bool(hit), ads_status="unknown",
        )
    if not running_ads:
        return skip("M3", "The business is not running paid social, so a pixel is not expected.",
                    pixel_present=bool(hit), ads_status="inactive")
    return result(
        "M3", bool(hit),
        "A tracking pixel is installed and paid social is running." if hit
        else "Paid social is running with no tracking pixel installed.",
        pixel_present=bool(hit), ads_status="active",
    )


@check("M4")
def m4_call_tracking(ctx: AuditContext) -> CheckResult:
    return _fingerprint_check("M4", ctx, CALL_TRACKING_FINGERPRINTS,
                              "Call tracking is in use.",
                              "No call tracking is in use.")
