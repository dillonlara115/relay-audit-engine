"""The SERP tool and the three checks it feeds.

F8, F9 and F12 are the only checks that look at the search page itself, so the
parsing boundary is pinned here: which block a result came from, whose domain
it belongs to, and what we are entitled to claim when an ad does not appear.
"""

from __future__ import annotations

from app.checks.base import AuditContext
from app.checks.extract import SiteFacts
from app.checks.serp import f8_map_pack, f9_organic, f12_paid_search
from app.status import FAIL, PASS, SKIPPED
from app.tools.serp import SerpFacts, SerpLook, flatten


def payload(*items, status_code=20000):
    return {"tasks": [{"status_code": status_code, "status_message": "Ok.",
                       "result": [{"items": list(items)}]}]}


def item(type_, domain, **extra):
    return {"type": type_, "domain": domain, **extra}


def ctx(serp, city="Colorado Springs"):
    return AuditContext(place={"city": city}, site=SiteFacts(homepage=None), serp=serp)


# ── parsing ───────────────────────────────────────────────────────────────────


def test_rank_is_counted_within_its_own_block():
    """Rank is the position a homeowner sees in that block, not the provider's
    absolute position, which counts every element on the page."""
    look = flatten(payload(
        item("local_pack", "other.com"),
        item("organic", "first.com"),
        item("local_pack", "triton.com"),
        item("organic", "second.com"),
        item("organic", "triton.com"),
    ), query="roofer Colorado Springs", location_name="x", domain="triton.com")

    assert look.ok
    assert look.map_pack_rank == 2     # second in the pack, not fourth on the page
    assert look.map_pack_size == 2
    assert look.organic_rank == 3
    assert look.organic_size == 3


def test_a_domain_that_never_appears_ranks_nowhere():
    look = flatten(payload(item("local_pack", "other.com"), item("organic", "rival.com")),
                   query="q", location_name="x", domain="triton.com")
    assert look.ok
    assert look.map_pack_rank is None
    assert look.organic_rank is None


def test_a_paid_slot_is_read_separately_from_organic():
    look = flatten(payload(item("paid", "triton.com"), item("organic", "rival.com")),
                   query="q", location_name="x", domain="triton.com")
    assert look.paid is True
    assert look.organic_rank is None, "an ad is not an organic position"


def test_a_rival_running_ads_is_not_our_ad():
    look = flatten(payload(item("paid", "rival.com")),
                   query="q", location_name="x", domain="triton.com")
    assert look.paid is False


def test_a_full_url_matches_the_same_domain_as_a_bare_one():
    look = flatten(payload(item("organic", None, url="https://www.triton.com/roofing")),
                   query="q", location_name="x", domain="triton.com")
    assert look.organic_rank == 1


def test_a_provider_error_is_an_error_not_an_empty_page():
    """An empty result and a failed task look identical downstream unless this
    holds. One means he ranks nowhere, the other means we never looked."""
    look = flatten({"tasks": [{"status_code": 40501, "status_message": "Invalid Field."}]},
                   query="q", location_name="x", domain="triton.com")
    assert not look.ok
    assert "40501" in look.error


def test_a_response_with_no_tasks_is_not_ok():
    assert not flatten({}, query="q", location_name="x", domain="d.com").ok


# ── thresholds ────────────────────────────────────────────────────────────────


def test_top_three_is_the_map_pack_line():
    assert SerpFacts(ok=True, map_pack_rank=3).in_map_pack
    assert not SerpFacts(ok=True, map_pack_rank=4).in_map_pack
    assert not SerpFacts(ok=True, map_pack_rank=None).in_map_pack


def test_top_ten_is_the_organic_line():
    assert SerpFacts(ok=True, organic_rank=10).in_organic
    assert not SerpFacts(ok=True, organic_rank=11).in_organic


# ── the checks ────────────────────────────────────────────────────────────────


def test_f8_passes_inside_the_pack_and_says_where():
    out = f8_map_pack(ctx(SerpFacts(ok=True, map_pack_rank=2)))
    assert out.status == PASS
    assert "number 2" in out.note and "Colorado Springs" in out.note


def test_f8_ranked_but_below_the_pack_fails_without_claiming_absence():
    out = f8_map_pack(ctx(SerpFacts(ok=True, map_pack_rank=5)))
    assert out.status == FAIL
    assert "number 5" in out.note
    assert "do not come up" not in out.note


def test_f9_past_the_first_page_fails():
    out = f9_organic(ctx(SerpFacts(ok=True, organic_rank=14)))
    assert out.status == FAIL
    assert "number 14" in out.note


def test_f12_never_claims_he_runs_no_ads_anywhere():
    """Two searches cannot prove a contractor buys no ads. An owner who is
    advertising on terms we did not search would read the flat claim as proof
    we never looked."""
    out = f12_paid_search(ctx(SerpFacts(ok=True, paid=False,
                                        queries=("roofer x", "roof replacement x"))))
    assert out.status == FAIL
    assert "may still be advertising on other searches" in out.note


def test_f12_passes_when_his_ad_ran():
    assert f12_paid_search(ctx(SerpFacts(ok=True, paid=True))).status == PASS


def test_all_three_skip_when_the_search_did_not_happen():
    """Rule 5. A contractor we never searched for is unknown, not unranked,
    and scoring him out of a map pack he may be sitting in invents a number."""
    for check in (f8_map_pack, f9_organic, f12_paid_search):
        assert check(ctx(None)).status == SKIPPED
        assert check(ctx(SerpFacts(ok=False, error="no website"))).status == SKIPPED


def test_a_look_survives_a_round_trip_through_the_cache():
    look = SerpLook(ok=True, query="q", location_name="x", map_pack_rank=1,
                    map_pack_size=3, organic_rank=4, organic_size=10, paid=True)
    assert SerpLook(**look.to_dict()) == look


# ── what a missing provider costs ─────────────────────────────────────────────


def test_found_goes_partial_when_the_serp_provider_is_not_configured():
    """F8, F9 and F12 are 7 of Found's 25 enabled points. With no DataForSEO
    credentials all three skip, and 7/25 is 28 percent unmeasured against a 20
    percent threshold, so every Found section reads partial.

    Pinned because the cost is invisible in the code: turning these checks on
    without provisioning the provider flags every audit in the system as
    incomplete, and the operator cannot see why from the console.
    """
    from app.checks.definitions import CHECK_DEFINITIONS
    from app.scoring import compute, outcomes_from
    from app.status import PASS

    found = [d for d in CHECK_DEFINITIONS if d["section"] == "found" and d["enabled"]]
    serp_codes = {"F8", "F9", "F12"}

    # Everything in Found passes except the three that need a provider.
    measured = {d["code"]: PASS for d in found if d["code"] not in serp_codes}
    score = compute(outcomes_from(measured, CHECK_DEFINITIONS))
    section = score.sections["found"]

    assert section.basis == 25
    assert section.unmeasured == 7
    assert section.partial, "7 of 25 unmeasured is past the 20 percent threshold"
    assert "found" in score.partial_sections

    # Scoring stays fair: the score normalizes over what was measured rather
    # than counting an unrun check as a failure.
    assert section.earned == 18
    assert round(section.normalized) == 30

    # And Found going partial does not cost the prospect a segment. Only a thin
    # Booked section does that.
    with_serp = {**measured, **{c: PASS for c in serp_codes}}
    assert not compute(outcomes_from(with_serp, CHECK_DEFINITIONS)).sections["found"].partial
