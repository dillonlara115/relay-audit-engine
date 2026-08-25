"""Fit gate tests. Table-driven, per engine spec section 10.

The gate is a pure function, so every case here is a dict in and a string out.
No fixtures, no network, no Firestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.gate import (
    GATE_FAIL,
    GATE_PASS,
    GATE_REVIEW,
    GateInput,
    GateReason,
    evaluate,
    roll_up,
)
from app.markets import COLORADO_SPRINGS, resolve_market
from app.tools.site_signals import SiteSignals

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def signals(**overrides) -> SiteSignals:
    """A site that passes every site-derived gate check unless overridden."""
    base = dict(
        reachable=True,
        pages_crawled=6,
        mentions_residential=True,
        mentions_commercial=False,
        commercial_only=False,
        founded_year=2005,
        has_careers_page=True,
        has_team_page=True,
        named_crew=True,
        office_address="1200 Garden of the Gods Rd",
        owner_name="Dave Whitaker",
    )
    base.update(overrides)
    return SiteSignals(**base)


def gate_input(**overrides) -> GateInput:
    """A prospect that passes every gate check unless overridden."""
    base = dict(
        place_id="place-1",
        business_name="Front Range Roofing",
        market=COLORADO_SPRINGS,
        website_url="https://frontrangeroofing.com",
        review_count=120,
        rating=4.8,
        first_review_at=NOW - timedelta(days=1500),
        latest_review_at=NOW - timedelta(days=5),
        review_sample_size=5,
        gbp_phone="(719) 555-0142",
        city="Colorado Springs",
        state="CO",
        address="1200 Garden of the Gods Rd, Colorado Springs, CO",
        primary_type="roofing_contractor",
        business_status="OPERATIONAL",
        site=signals(),
        now=NOW,
    )
    base.update(overrides)
    return GateInput(**base)


def reason_for(verdict, code: str):
    return next(r for r in verdict.reasons if r.code == code)


# ── The happy path ────────────────────────────────────────────────────────────


def test_clean_prospect_passes():
    verdict = evaluate(gate_input())
    assert verdict.result == GATE_PASS
    assert verdict.continues
    assert all(r.verdict == "pass" for r in verdict.reasons)


def test_every_check_is_reported_even_when_passing():
    """The report needs the whole tally, not just the failures."""
    verdict = evaluate(gate_input())
    assert len(verdict.reasons) == 9
    assert len({r.code for r in verdict.reasons}) == 9


# ── Blocking failures ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides, code, note",
    [
        ({"review_count": 24}, "revenue_proxy_reviews", "one under the floor"),
        ({"review_count": 0}, "revenue_proxy_reviews", "no reviews"),
        (
            {"site": signals(mentions_residential=False, mentions_commercial=True, commercial_only=True)},
            "commercial_only",
            "commercial exclusive",
        ),
        ({"city": "Pueblo", "state": "CO"}, "real_local_operator", "outside the metro"),
        ({"state": "WY", "city": "Cheyenne"}, "real_local_operator", "out of state"),
        ({"business_status": "CLOSED_PERMANENTLY"}, "real_local_operator", "closed"),
        ({"territory_conflict": True}, "territory_clear", "client overlap"),
    ],
)
def test_blocking_failures_fail_the_gate(overrides, code, note):
    verdict = evaluate(gate_input(**overrides))
    assert verdict.result == GATE_FAIL, note
    assert reason_for(verdict, code).verdict == "fail"
    assert not verdict.continues


def test_storm_chaser_with_a_complete_review_history_fails():
    """All reviews inside a short window, and we sampled all of them."""
    verdict = evaluate(
        gate_input(
            review_count=30,
            review_sample_size=30,
            first_review_at=NOW - timedelta(days=200),
            latest_review_at=NOW - timedelta(days=10),
        )
    )
    assert verdict.result == GATE_FAIL
    assert reason_for(verdict, "not_storm_chaser").verdict == "fail"


def test_local_address_with_a_foreign_area_code_fails():
    verdict = evaluate(gate_input(gbp_phone="(212) 555-0142"))
    assert verdict.result == GATE_FAIL
    assert reason_for(verdict, "real_local_operator").verdict == "fail"


# ── The truncated-sample guard ────────────────────────────────────────────────


def test_truncated_review_sample_does_not_convict():
    """Places returns five reviews. A short span across five proves nothing.

    This is the case that must not fail, because it describes almost every
    real prospect and an earlier rule would have failed all of them.
    """
    verdict = evaluate(
        gate_input(
            review_count=120,
            review_sample_size=5,
            first_review_at=NOW - timedelta(days=60),
            latest_review_at=NOW - timedelta(days=2),
        )
    )
    reason = reason_for(verdict, "not_storm_chaser")
    assert reason.verdict == "unknown"
    assert reason.severity == "advisory"
    assert verdict.result == GATE_PASS, "an advisory unknown must not move the result"


# ── REVIEW, not FAIL ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"site": signals(owner_name=None)}, "reachable_owner"),
        (
            {"site": signals(has_careers_page=False, has_team_page=False, named_crew=False, office_address=None)},
            "revenue_proxy_substance",
        ),
        ({"site": signals(founded_year=2023)}, "revenue_proxy_tenure"),
    ],
)
def test_advisory_failures_route_to_review(overrides, code):
    verdict = evaluate(gate_input(**overrides))
    assert verdict.result == GATE_REVIEW
    assert reason_for(verdict, code).verdict == "fail"
    assert verdict.continues, "REVIEW still gets audited"


def test_no_website_routes_to_review_not_fail():
    """No site is a gap, not a disqualification. A human decides."""
    verdict = evaluate(gate_input(website_url=None, site=None))
    assert verdict.result == GATE_REVIEW
    assert verdict.continues
    assert reason_for(verdict, "commercial_only").verdict == "unknown"


def test_unknown_locality_in_an_unmapped_metro_reviews():
    verdict = evaluate(
        gate_input(market=resolve_market("Boise"), city="Boise", state="ID", gbp_phone="(208) 555-0142")
    )
    assert verdict.result == GATE_REVIEW
    assert reason_for(verdict, "real_local_operator").verdict == "unknown"


# ── Incumbent agency ──────────────────────────────────────────────────────────


def test_incumbent_agency_is_surfaced_without_changing_the_verdict():
    """The Burned Skeptic tag changes the opener, never the gate."""
    verdict = evaluate(gate_input(site=signals(incumbent_agency="scorpion")))
    assert verdict.incumbent_agency == "scorpion"
    assert verdict.result == GATE_PASS


# ── The rollup itself ─────────────────────────────────────────────────────────


def reasons(*specs) -> list[GateReason]:
    return [
        GateReason(code=f"c{i}", label=f"c{i}", verdict=v, severity=s, detail="")
        for i, (v, s) in enumerate(specs)
    ]


@pytest.mark.parametrize(
    "specs, expected",
    [
        ((("pass", "blocking"), ("pass", "advisory")), GATE_PASS),
        ((("pass", "blocking"), ("unknown", "advisory")), GATE_PASS),
        ((("fail", "blocking"), ("pass", "advisory")), GATE_FAIL),
        ((("fail", "blocking"), ("fail", "advisory")), GATE_FAIL),
        ((("unknown", "blocking"), ("pass", "advisory")), GATE_REVIEW),
        ((("pass", "blocking"), ("fail", "advisory")), GATE_REVIEW),
        ((("unknown", "blocking"), ("fail", "blocking")), GATE_FAIL),
    ],
)
def test_rollup_precedence(specs, expected):
    assert roll_up(reasons(*specs)) == expected


def test_rollup_of_nothing_is_a_pass():
    assert roll_up([]) == GATE_PASS


def test_informational_codes_are_recorded_but_never_move_the_result(monkeypatch):
    """The escape hatch for a check that is worth knowing and not worth gating."""
    demoted = [GateReason(code="reachable_owner", label="Reachable owner",
                          verdict="fail", severity="advisory", detail="")]
    assert roll_up(demoted) == GATE_REVIEW

    monkeypatch.setattr("app.gate.INFORMATIONAL_CODES", frozenset({"reachable_owner"}))
    assert roll_up(demoted) == GATE_PASS

    # Still evaluated and still reported, just not counted.
    verdict = evaluate(gate_input(site=signals(owner_name=None)))
    assert verdict.result == GATE_PASS
    assert reason_for(verdict, "reachable_owner").verdict == "fail"
