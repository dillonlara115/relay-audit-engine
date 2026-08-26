"""The 44 check definitions, verbatim from the criteria doc.

These are seeded into Firestore and read from there at audit time. The engine
spec is explicit that weights get retuned after the first batch and that a
retune must be a document edit rather than a deploy, so this file is the seed
and Firestore is the source of truth once seeded.

Two states are easy to confuse and must not be:

- `enabled=False`   we are not running this check this week. It never appears in
                    an audit and never lands in a section denominator.
- status `skipped`  the check is enabled and could not run on this prospect,
                    because robots disallowed the page or the renderer was down.
                    It leaves the denominator for that prospect only, and enough
                    of them mark the audit `partial`.

Points sum to 30 / 30 / 40 across the three scored sections. Asserted at import.
"""

from __future__ import annotations

from typing import Any

FOUND = "found"
CHOSEN = "chosen"
BOOKED = "booked"
MEASUREMENT = "measurement"

SECTION_WEIGHTS: dict[str, int] = {FOUND: 30, CHOSEN: 30, BOOKED: 40}

# Reasons a check is off this week. Spelled out so a future reader knows whether
# it was scope, cost, or a missing credential.
_AFTER_AUG_31 = "SERP provider is [after Aug 31] in the engine spec."
_MANUAL = "Manual check, cut list item 1."
_NO_TOKEN = "Needs META_ADS_ACCESS_TOKEN, which is not provisioned. Cut list item 3."


def _check(
    code: str,
    section: str,
    title: str,
    full_credit: str,
    points: int,
    source: str,
    automation: str,
    sort_order: int,
    *,
    enabled: bool = True,
    disabled_reason: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": code,
        "section": section,
        "title": title,
        "full_credit": full_credit,
        "points": points,
        "source": source,
        "automation": automation,
        "sort_order": sort_order,
        "enabled": enabled,
    }
    if disabled_reason:
        row["disabled_reason"] = disabled_reason
    if note:
        row["note"] = note
    return row


# ── FOUND, 30 points ──────────────────────────────────────────────────────────

_FOUND: list[dict[str, Any]] = [
    _check("F1", FOUND, "GBP claimed and verified", "Claimed", 2, "Places", "manual", 10,
           enabled=False,
           disabled_reason="Places API (New) exposes no claimed or verified field.",
           note="Decide before scoring ships: manual, or off the Found denominator."),
    _check("F2", FOUND, "Primary category", "Roofing Contractor", 1, "Places", "auto", 20),
    _check("F3", FOUND, "Review count", "50+", 3, "Places", "auto", 30),
    _check("F4", FOUND, "Average rating", "4.5+", 2, "Places", "auto", 40),
    _check("F5", FOUND, "Review recency", "Newest within 30 days", 3, "Places", "auto", 50),
    _check("F6", FOUND, "Photo recency", "Newest within 90 days", 1, "Places", "manual", 60,
           enabled=False,
           disabled_reason="Places API (New) photos carry no timestamp, only name, "
                           "dimensions and author attribution.",
           note="Not automatable from Places. Visible on the profile to a human, "
                "so it survives as a manual check or comes off the Found denominator."),
    _check("F7", FOUND, "Phone match", "Site phone matches GBP phone", 3, "Site + Places", "auto", 70,
           note="Invisible gap check. Roughly a third of local businesses fail it."),
    _check("F8", FOUND, "Map pack presence", "Top 3 for roofer [city]", 3, "SERP", "auto", 80,
           enabled=False, disabled_reason=_AFTER_AUG_31),
    _check("F9", FOUND, "Organic presence", "Top 10 for roof replacement [city]", 3, "SERP", "auto", 90,
           enabled=False, disabled_reason=_AFTER_AUG_31),
    _check("F10", FOUND, "Service area coverage", "3+ city pages indexed", 2, "Crawl", "auto", 100),
    _check("F11", FOUND, "Content freshness", "Any page in last 180 days", 1, "Crawl", "auto", 110),
    _check("F12", FOUND, "Paid search", "Running Google Ads", 1, "SERP", "auto", 120,
           enabled=False, disabled_reason=_AFTER_AUG_31),
    _check("F13", FOUND, "Paid social", "Active in Meta Ad Library", 1, "Ad Library", "auto", 130,
           enabled=False, disabled_reason=_NO_TOKEN),
    _check("F14", FOUND, "Machine-readable reviews", "Review or AggregateRating schema", 2, "Crawl", "auto", 140,
           note="Invisible gap check. Close to nine in ten carry none."),
    _check("F15", FOUND, "Business schema", "LocalBusiness with correct NAP", 1, "Crawl", "auto", 150),
    _check("F16", FOUND, "AI answer presence", "Named for best roofer [city]", 1, "Manual", "manual", 160,
           enabled=False, disabled_reason=_MANUAL),
]

# ── CHOSEN, 30 points ─────────────────────────────────────────────────────────

_CHOSEN: list[dict[str, Any]] = [
    _check("C1", CHOSEN, "Site loads", "200 on https", 2, "Fetch", "auto", 200),
    _check("C2", CHOSEN, "Mobile usable", "No horizontal scroll, 16px+ text", 2, "Render 390px", "auto", 210),
    _check("C3", CHOSEN, "Mobile speed", "PSI mobile 60+", 2, "PSI", "auto", 220),
    _check("C4", CHOSEN, "LCP", "Under 2.5s mobile", 2, "PSI", "auto", 230),
    _check("C5", CHOSEN, "Phone above fold", "Visible without scrolling", 3, "Render", "auto", 240),
    _check("C6", CHOSEN, "Click-to-call", "tel: on the header number", 2, "Crawl", "auto", 250),
    _check("C7", CHOSEN, "Form above fold", "Or one visible primary CTA", 2, "Render", "auto", 260),
    _check("C8", CHOSEN, "Form friction", "5 fields or fewer", 2, "Crawl", "auto", 270),
    _check("C9", CHOSEN, "Real project photos", "Own photos, not stock", 2, "Vision", "auto", 280,
           enabled=False,
           disabled_reason="Cut list item 2. The vision screenshot is clipped to 6000px and "
                           "roofing homepages measured 12500 to 15500px, so the model sees "
                           "roughly the top half and never reaches the project gallery.",
           note="Measured 2026-08-26: failed 5 of 5 real sites, with reasons citing the hero "
                "background. Re-enable behind a taller or two image screenshot, not before. "
                "The vision component still records stock_photos as diagnostic evidence."),
    _check("C10", CHOSEN, "Reviews on page", "Testimonials on home", 2, "Crawl", "auto", 290),
    _check("C11", CHOSEN, "Licensed / insured", "Stated on site", 1, "Crawl", "auto", 300),
    _check("C12", CHOSEN, "Manufacturer credential", "GAF, Owens Corning, CertainTeed tier", 2, "Crawl", "auto", 310,
           note="Cut list item 4."),
    _check("C13", CHOSEN, "Warranty terms", "Workmanship warranty stated", 1, "Crawl", "auto", 320),
    _check("C14", CHOSEN, "Financing", "Mentioned or applied for on site", 1, "Crawl", "auto", 330),
    _check("C15", CHOSEN, "Insurance / storm page", "Dedicated claim-help page", 2, "Crawl", "auto", 340,
           note="Front Range weight. Hail claims are the buying trigger here."),
    _check("C16", CHOSEN, "Footer copyright", "Current or last year", 1, "Crawl", "auto", 350),
    _check("C17", CHOSEN, "Trust read", "Vision verdict of adequate or better", 1, "Vision", "auto", 360),
]

# ── BOOKED, 40 points ─────────────────────────────────────────────────────────

_BOOKED: list[dict[str, Any]] = [
    _check("B1", BOOKED, "Self-serve booking", "Homeowner can pick a time without waiting", 10, "Crawl", "auto", 400),
    _check("B2", BOOKED, "Form health", "Form has an action, validates, resolves without error", 8, "Render", "auto", 410,
           note="The sleeper. Fill to test validation. Never submit."),
    _check("B3", BOOKED, "Missed-call text-back", "Detected on the primary number", 6, "Crawl", "auto", 420),
    _check("B4", BOOKED, "Live chat", "Widget present and configured", 4, "Crawl", "auto", 430,
           note="Cut list item 5."),
    _check("B5", BOOKED, "Response promise", "A stated response time anywhere on site", 4, "Crawl", "auto", 440,
           note="Cut list item 5."),
    _check("B6", BOOKED, "Confirmation clarity", "Thank-you state tells him what happens next", 4, "Render", "auto", 450,
           enabled=False,
           disabled_reason="Unmeasurable without submitting the form, which is hard rule 1. "
                           "Measured 2026-08-26 across a 40 prospect batch: skipped on 38, "
                           "because confirmation states are almost always inline.",
           note="A v1 relic: its Render source assumed the probe was still in the pipeline. "
                "Its 4 unmeasured points pushed every prospect 10% toward the partial line, "
                "which with any second skip crossed 20% and unsegmented 22 of 40 audits. "
                "The implementation stays: sites exposing a /thank-you page still get the "
                "diagnostic read if re-enabled."),
    _check("B7", BOOKED, "After-hours coverage", "Hours published and an after-hours path stated", 4, "Crawl + Places", "auto", 460),
]

# ── Measurement layer, 0 points, always recorded ──────────────────────────────

_MEASUREMENT: list[dict[str, Any]] = [
    _check("M1", MEASUREMENT, "GA4 installed", "Detected on the site", 0, "Crawl", "auto", 500),
    _check("M2", MEASUREMENT, "Google Ads conversion tag", "Detected on the site", 0, "Crawl", "auto", 510),
    _check("M3", MEASUREMENT, "Meta pixel", "Present while running Meta ads", 0, "Crawl + Ad Library", "auto", 520,
           note="Only a finding when F13 confirmed active ads. No ads means no finding."),
    _check("M4", MEASUREMENT, "Call tracking number in use", "Detected on the site", 0, "Crawl", "auto", 530),
]

CHECK_DEFINITIONS: list[dict[str, Any]] = [*_FOUND, *_CHOSEN, *_BOOKED, *_MEASUREMENT]


def by_code() -> dict[str, dict[str, Any]]:
    return {row["code"]: row for row in CHECK_DEFINITIONS}


def section_points(section: str, *, enabled_only: bool = False) -> int:
    return sum(
        row["points"]
        for row in CHECK_DEFINITIONS
        if row["section"] == section and (row["enabled"] or not enabled_only)
    )


def _assert_weights() -> None:
    """The nominal weights are the contract. A typo here silently reweights
    every audit we run, so it fails at import rather than at score time."""
    for section, weight in SECTION_WEIGHTS.items():
        total = section_points(section)
        if total != weight:
            raise AssertionError(f"{section} check points sum to {total}, expected {weight}")
    codes = [row["code"] for row in CHECK_DEFINITIONS]
    if len(codes) != len(set(codes)):
        raise AssertionError("duplicate check code in CHECK_DEFINITIONS")


_assert_weights()
