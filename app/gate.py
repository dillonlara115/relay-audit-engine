"""The fit gate. Criteria doc section 0, implemented as a pure function.

No I/O. Everything it needs arrives in `GateInput`, which is what makes it
table-testable and what makes the reasons reproducible six weeks from now.

Verdict rules
-------------
Every gate check returns pass, fail, or unknown, and carries a severity.

- Any BLOCKING fail            -> FAIL   (no audit runs)
- Otherwise any non-pass       -> REVIEW (audit runs, a human looks at reasons)
- Otherwise                    -> PASS

Unknown is never silently treated as pass. Guardrail 4: absent is absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.markets import MarketSpec
from app.tools.phones import parse_phone
from app.tools.site_signals import SiteSignals

Verdict = Literal["pass", "fail", "unknown"]
Severity = Literal["blocking", "advisory"]

GATE_PASS = "pass"
GATE_REVIEW = "review"
GATE_FAIL = "fail"

MIN_REVIEWS = 25
MIN_YEARS_IN_BUSINESS = 5
MIN_REVIEW_SPAN_DAYS = 730  # 24 months


@dataclass(frozen=True)
class GateReason:
    code: str
    label: str
    verdict: Verdict
    severity: Severity
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "verdict": self.verdict,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateInput:
    place_id: str
    business_name: str
    market: MarketSpec
    website_url: str | None = None
    review_count: int | None = None
    rating: float | None = None
    first_review_at: datetime | None = None
    latest_review_at: datetime | None = None
    review_sample_size: int | None = None
    gbp_phone: str | None = None
    city: str | None = None
    state: str | None = None
    address: str | None = None
    primary_type: str | None = None
    types: tuple[str, ...] = ()
    business_status: str | None = None
    site: SiteSignals | None = None
    territory_conflict: bool = False
    territory_conflict_detail: str | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class GateVerdict:
    result: str
    reasons: list[GateReason]
    incumbent_agency: str | None = None

    @property
    def continues(self) -> bool:
        return self.result in (GATE_PASS, GATE_REVIEW)

    def failures(self) -> list[GateReason]:
        return [r for r in self.reasons if r.verdict == "fail"]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.reasons]


ROOFING_TYPES = frozenset({"roofing_contractor", "roofing_supply_store"})
DISQUALIFYING_TYPES = frozenset(
    {"roofing_supply_store", "hardware_store", "home_goods_store", "insurance_agency"}
)


def _reason(code, label, verdict, severity, detail) -> GateReason:
    return GateReason(code=code, label=label, verdict=verdict, severity=severity, detail=detail)


# ── Individual checks ─────────────────────────────────────────────────────────


def _residential_work(gi: GateInput) -> GateReason:
    site = gi.site
    category_roofing = gi.primary_type in ROOFING_TYPES or any(
        t in ROOFING_TYPES for t in gi.types
    )
    if site and site.mentions_residential:
        return _reason(
            "residential_work", "Residential work", "pass", "blocking",
            "Site copy describes residential roofing.",
        )
    if site and site.evidence_available() and site.mentions_commercial and not site.mentions_residential:
        return _reason(
            "residential_work", "Residential work", "fail", "blocking",
            "Site copy describes commercial roofing only.",
        )
    if category_roofing:
        return _reason(
            "residential_work", "Residential work", "pass", "blocking",
            f"Google category is {gi.primary_type or 'roofing'}, no commercial-only signal.",
        )
    return _reason(
        "residential_work", "Residential work", "unknown", "blocking",
        "No residential signal in the Google category or on the site.",
    )


def _commercial_only(gi: GateInput) -> GateReason:
    site = gi.site
    if site is None or not site.evidence_available():
        return _reason(
            "commercial_only", "Not commercial-exclusive", "unknown", "blocking",
            "No site copy available to read.",
        )
    if site.commercial_only:
        return _reason(
            "commercial_only", "Not commercial-exclusive", "fail", "blocking",
            "Site reads as commercial-exclusive.",
        )
    return _reason(
        "commercial_only", "Not commercial-exclusive", "pass", "blocking",
        "Site is not commercial-exclusive.",
    )


def _revenue_proxy_reviews(gi: GateInput) -> GateReason:
    if gi.review_count is None:
        return _reason(
            "revenue_proxy_reviews", f"{MIN_REVIEWS}+ Google reviews", "unknown", "blocking",
            "Review count not returned by Places.",
        )
    if gi.review_count >= MIN_REVIEWS:
        return _reason(
            "revenue_proxy_reviews", f"{MIN_REVIEWS}+ Google reviews", "pass", "blocking",
            f"{gi.review_count} reviews.",
        )
    return _reason(
        "revenue_proxy_reviews", f"{MIN_REVIEWS}+ Google reviews", "fail", "blocking",
        f"{gi.review_count} reviews, under the {MIN_REVIEWS} floor.",
    )


def _revenue_proxy_tenure(gi: GateInput) -> GateReason:
    site = gi.site
    years: int | None = None
    source = ""
    if site and site.founded_year:
        years = gi.now.year - site.founded_year
        source = f"site states since {site.founded_year}"
    if site and site.years_in_business and (years is None or site.years_in_business > years):
        years = site.years_in_business
        source = f"site states {site.years_in_business} years"

    if years is None:
        return _reason(
            "revenue_proxy_tenure", f"{MIN_YEARS_IN_BUSINESS}+ years in business", "unknown",
            "advisory", "No founding year or tenure claim found.",
        )
    if years >= MIN_YEARS_IN_BUSINESS:
        return _reason(
            "revenue_proxy_tenure", f"{MIN_YEARS_IN_BUSINESS}+ years in business", "pass",
            "advisory", f"About {years} years, {source}.",
        )
    return _reason(
        "revenue_proxy_tenure", f"{MIN_YEARS_IN_BUSINESS}+ years in business", "fail",
        "advisory", f"About {years} years, {source}.",
    )


def _revenue_proxy_substance(gi: GateInput) -> GateReason:
    site = gi.site
    label = "Careers page, named crew, or office address"
    if site is None or not site.evidence_available():
        return _reason(
            "revenue_proxy_substance", label, "unknown", "advisory",
            "No site copy available to read.",
        )
    hits = []
    if site.has_careers_page:
        hits.append("careers or hiring page")
    if site.named_crew or site.has_team_page:
        hits.append("named crew or team page")
    if site.office_address:
        hits.append(f"office address ({site.office_address})")
    if hits:
        return _reason("revenue_proxy_substance", label, "pass", "advisory", "Found " + ", ".join(hits) + ".")
    return _reason(
        "revenue_proxy_substance", label, "fail", "advisory",
        "No careers page, named crew, or office address on the site. Fleet photos are not machine-checked.",
    )


def _real_local_operator(gi: GateInput) -> GateReason:
    label = "Real local operator"
    if gi.business_status and gi.business_status != "OPERATIONAL":
        return _reason(
            "real_local_operator", label, "fail", "blocking",
            f"Google reports the business as {gi.business_status.lower().replace('_', ' ')}.",
        )

    in_metro = gi.market.in_metro(gi.city, gi.state)
    phone = parse_phone(gi.gbp_phone)
    local_code = gi.market.local_area_code(phone.area_code if phone else None)

    if in_metro is False:
        return _reason(
            "real_local_operator", label, "fail", "blocking",
            f"Address in {gi.city or 'an unknown city'}, {gi.state or '??'}, outside the {gi.market.name} metro.",
        )
    if in_metro is None:
        detail = "Metro boundaries not enumerated for this market." if not gi.market.boundaries_known else "No locality returned by Places."
        return _reason("real_local_operator", label, "unknown", "blocking", detail)
    if local_code is False:
        return _reason(
            "real_local_operator", label, "fail", "blocking",
            f"Local address in {gi.city}, but the Google number is a {phone.area_code} line.",
        )
    if phone is None:
        return _reason(
            "real_local_operator", label, "unknown", "blocking",
            f"Local address in {gi.city}, but no usable phone number on the profile.",
        )
    return _reason(
        "real_local_operator", label, "pass", "blocking",
        f"Address in {gi.city}, {gi.state} with a {phone.area_code} number.",
    )


def _not_storm_chaser(gi: GateInput) -> GateReason:
    label = "Not a storm chaser"
    if gi.first_review_at and gi.latest_review_at:
        span_days = (gi.latest_review_at - gi.first_review_at).days
        if span_days >= MIN_REVIEW_SPAN_DAYS:
            return _reason(
                "not_storm_chaser", label, "pass", "blocking",
                f"Reviews span about {span_days // 30} months.",
            )
        # Places returns at most five reviews. A short span across a truncated
        # sample proves nothing, so we decline to conclude rather than fail a
        # real operator on a sampling artifact.
        truncated = (
            gi.review_sample_size is not None
            and gi.review_count is not None
            and gi.review_count > gi.review_sample_size
        )
        if truncated:
            return _reason(
                "not_storm_chaser", label, "unknown", "advisory",
                f"Sampled {gi.review_sample_size} of {gi.review_count} reviews spanning "
                f"about {max(span_days // 30, 0)} months. Sample is truncated, history not established.",
            )
        return _reason(
            "not_storm_chaser", label, "fail", "blocking",
            f"All {gi.review_count} reviews fall inside about {max(span_days // 30, 0)} months.",
        )
    return _reason(
        "not_storm_chaser", label, "unknown", "advisory",
        "No review timestamps returned, history not established.",
    )


def _territory_clear(gi: GateInput) -> GateReason:
    label = "Territory clear"
    if gi.territory_conflict:
        return _reason(
            "territory_clear", label, "fail", "blocking",
            gi.territory_conflict_detail or "Overlaps an active client's area.",
        )
    return _reason("territory_clear", label, "pass", "blocking", "No active client overlap.")


def _reachable_owner(gi: GateInput) -> GateReason:
    label = "Reachable owner"
    site = gi.site
    if site and site.owner_name:
        return _reason("reachable_owner", label, "pass", "advisory", f"Named on the site: {site.owner_name}.")
    if site is None or not site.evidence_available():
        return _reason("reachable_owner", label, "unknown", "advisory", "No site copy available to read.")
    return _reason("reachable_owner", label, "fail", "advisory", "No owner or GM named on the site.")


CHECKS = (
    _residential_work,
    _commercial_only,
    _revenue_proxy_reviews,
    _revenue_proxy_tenure,
    _revenue_proxy_substance,
    _real_local_operator,
    _not_storm_chaser,
    _territory_clear,
    _reachable_owner,
)


# Codes listed here are still evaluated, still recorded, and still shown to the
# operator, but they never move the gate result.
#
# TODO(dillon): decide whether "reachable_owner" belongs here.
#
# Measured on the Colorado Springs sweep of 2026-08-25, 80 prospects:
#   empty set (as shipped)        pass 16   review 55   fail 9
#   {"reachable_owner"}           pass 61   review 10   fail 9
#
# 45 of those 55 REVIEWs are in the queue for no reason other than "no owner or
# GM named on the site". The question is whether a missing owner name is a fit
# problem or an enrichment task. If you would still call a 150-review roofer
# whose site never names him, and just look the name up before dialling, it is
# enrichment and it belongs in this set. If a roofer who will not put his name
# on his own site is genuinely a worse prospect to you, leave the set empty and
# keep working the 55.
#
# Left empty on purpose: over-reviewing is recoverable, auto-passing 45
# prospects on a judgement that was not yours is not.
INFORMATIONAL_CODES: frozenset[str] = frozenset()


def roll_up(reasons: list[GateReason]) -> str:
    """Nine check verdicts to one routing decision.

    Severity matters as much as verdict here. An earlier version treated any
    non-pass as REVIEW, which sounded conservative and was useless in practice:
    Places returns at most five reviews, so "Not a storm chaser" is advisory
    unknown on almost every real prospect, and every prospect landed in REVIEW.
    A queue that contains everything is not a queue.

    So an unknown is read against what it would have told us:

    - blocking fail     -> FAIL    disqualifying, no audit runs
    - blocking unknown  -> REVIEW  we could not verify something that decides fit
    - advisory fail     -> REVIEW  a real miss, but not disqualifying on its own
    - advisory unknown  -> ignored we accept this is not observable from outside

    Advisory unknown is the only verdict that does not move the result, and it
    is deliberate: an absent signal is not evidence against a prospect. The
    reason is still recorded either way, so nothing is hidden from the operator.
    """
    counted = [r for r in reasons if r.code not in INFORMATIONAL_CODES]

    if any(r.verdict == "fail" and r.severity == "blocking" for r in counted):
        return GATE_FAIL
    if any(r.verdict == "unknown" and r.severity == "blocking" for r in counted):
        return GATE_REVIEW
    if any(r.verdict == "fail" for r in counted):
        return GATE_REVIEW
    return GATE_PASS


def evaluate(gi: GateInput) -> GateVerdict:
    """Pure. Same input, same verdict, forever."""
    reasons = [check(gi) for check in CHECKS]
    return GateVerdict(
        result=roll_up(reasons),
        reasons=reasons,
        incumbent_agency=gi.site.incumbent_agency if gi.site else None,
    )
