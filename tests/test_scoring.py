"""Scoring tests. Table-driven, per engine spec section 10.

Every band boundary, every segment rule, partial handling. This is the module
that gets retuned, so the tests are the spec.
"""

from __future__ import annotations

import pytest

from app.checks.definitions import BOOKED, CHECK_DEFINITIONS, CHOSEN, FOUND, MEASUREMENT
from app.scoring import (
    BAND_BROKEN,
    BAND_DIALED,
    BAND_LEAKING,
    BAND_TUNED,
    ERROR,
    FAIL,
    PASS,
    SEG_BOTH_BROKEN,
    SEG_DIALED,
    SEG_INVISIBLE_PRO,
    SEG_LEAKY_BUCKET,
    SKIPPED,
    UNSEGMENTED_PRIORITY,
    CheckOutcome,
    Score,
    band_for,
    compute,
    outcomes_from,
    segment_for,
)


def outcomes(section: str, *specs: tuple[int, str]) -> list[CheckOutcome]:
    """(points, status) pairs into outcomes for one section."""
    return [
        CheckOutcome(code=f"{section[0].upper()}{i}", section=section, status=status, points=points)
        for i, (points, status) in enumerate(specs, start=1)
    ]


def full(section: str, points: int, status: str) -> list[CheckOutcome]:
    """One check worth the section's whole nominal weight."""
    return outcomes(section, (points, status))


def build(found_pts=30, found_status=PASS, chosen_pts=30, chosen_status=PASS,
          booked_pts=40, booked_status=PASS) -> Score:
    return compute(
        full(FOUND, found_pts, found_status)
        + full(CHOSEN, chosen_pts, chosen_status)
        + full(BOOKED, booked_pts, booked_status)
    )


# ── Bands, every boundary ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "total, band",
    [
        (100, BAND_DIALED),
        (86, BAND_DIALED),
        (85, BAND_DIALED),
        (84, BAND_TUNED),
        (66, BAND_TUNED),
        (65, BAND_TUNED),
        (64, BAND_LEAKING),
        (41, BAND_LEAKING),
        (40, BAND_LEAKING),
        (39, BAND_BROKEN),
        (1, BAND_BROKEN),
        (0, BAND_BROKEN),
    ],
)
def test_band_boundaries(total, band):
    assert band_for(total) == band


def test_band_below_zero_is_broken():
    assert band_for(-1) == BAND_BROKEN


# ── Segments, every rule ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "found, booked, segment, note",
    [
        (20.0, 20.0, SEG_LEAKY_BUCKET, "both thresholds exactly"),
        (30.0, 0.0, SEG_LEAKY_BUCKET, "pays for demand, catches nothing"),
        (25.0, 12.0, SEG_LEAKY_BUCKET, "the wedge"),
        (15.0, 28.0, SEG_INVISIBLE_PRO, "both thresholds exactly"),
        (0.0, 40.0, SEG_INVISIBLE_PRO, "converts, invisible"),
        (10.0, 35.0, SEG_INVISIBLE_PRO, "visibility sale"),
        (15.0, 20.0, SEG_BOTH_BROKEN, "both thresholds exactly"),
        (0.0, 0.0, SEG_BOTH_BROKEN, "full rebuild"),
        (20.0, 28.0, SEG_DIALED, "both thresholds exactly"),
        (30.0, 40.0, SEG_DIALED, "referral partner"),
    ],
)
def test_segment_signatures(found, booked, segment, note):
    assert segment_for(found, booked) == segment, note


@pytest.mark.parametrize(
    "found, booked",
    [
        (18.0, 24.0),   # neither axis is high or low
        (16.0, 21.0),   # just inside both middles
        (19.9, 20.0),   # a hair under Found high, Booked low
        (25.0, 24.0),   # Found high, Booked in the middle
        (18.0, 30.0),   # Booked high, Found in the middle
        (15.0, 24.0),   # Found low, Booked in the middle
    ],
)
def test_the_uncovered_middle_is_none_not_a_guess(found, booked):
    """The four signatures do not cover the plane. That gap stays visible."""
    assert segment_for(found, booked) is None


def test_unsegmented_sorts_last():
    score = compute(
        full(FOUND, 30, PASS) + full(CHOSEN, 30, PASS) + outcomes(BOOKED, (40, FAIL), (0, PASS))
    )
    middle = Score(0, 0, 0, 0, 0, 0, 0, BAND_BROKEN, None, False)
    assert middle.segment_priority == UNSEGMENTED_PRIORITY
    assert score.segment_priority <= UNSEGMENTED_PRIORITY


# ── Section arithmetic ────────────────────────────────────────────────────────


def test_all_pass_is_one_hundred():
    score = build()
    assert (score.total, score.band, score.segment) == (100, BAND_DIALED, SEG_DIALED)
    assert not score.partial


def test_all_fail_is_zero():
    score = build(found_status=FAIL, chosen_status=FAIL, booked_status=FAIL)
    assert (score.total, score.band, score.segment) == (0, BAND_BROKEN, SEG_BOTH_BROKEN)


def test_sections_normalize_to_their_nominal_weight():
    """Half of Found is 15 of 100, not half of 100."""
    score = compute(
        outcomes(FOUND, (15, PASS), (15, FAIL))
        + full(CHOSEN, 30, FAIL)
        + full(BOOKED, 40, FAIL)
    )
    assert score.normalized(FOUND) == pytest.approx(15.0)
    assert score.total == 15


def test_a_short_section_still_normalizes_to_full_weight():
    """Booked measured on 10 of 40 points, all passing, is a full 40."""
    score = compute(
        full(FOUND, 30, FAIL)
        + full(CHOSEN, 30, FAIL)
        + outcomes(BOOKED, (10, PASS), (30, SKIPPED))
    )
    assert score.booked == 10
    assert score.booked_max == 10
    assert score.normalized(BOOKED) == pytest.approx(40.0)
    assert score.partial, "and it must be flagged, which is the whole point"


def test_max_excludes_skipped_and_error():
    score = compute(
        outcomes(FOUND, (10, PASS), (10, SKIPPED), (10, ERROR))
        + full(CHOSEN, 30, PASS)
        + full(BOOKED, 40, PASS)
    )
    assert score.found == 10
    assert score.found_max == 10, "skipped and errored points leave the denominator"
    assert score.sections[FOUND].basis == 30
    assert score.sections[FOUND].unmeasured == 20


def test_a_fully_unmeasured_section_scores_zero_not_full_marks():
    """Dividing nothing by nothing must not read as a perfect section."""
    score = compute(
        full(FOUND, 30, SKIPPED) + full(CHOSEN, 30, PASS) + full(BOOKED, 40, PASS)
    )
    assert score.normalized(FOUND) == 0.0
    assert score.found_max == 0
    assert score.total == 70
    assert score.partial


def test_total_is_clamped_to_a_hundred():
    assert build().total <= 100


# ── Partial handling ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "skipped, basis, expected_partial",
    [
        (0, 40, False),
        (8, 40, False),   # exactly 20%, not more than
        (9, 40, True),    # over the line
        (40, 40, True),
    ],
)
def test_partial_threshold_is_strictly_more_than_twenty_percent(skipped, basis, expected_partial):
    kept = basis - skipped
    specs = [(kept, PASS)] if kept else []
    if skipped:
        specs.append((skipped, SKIPPED))
    score = compute(full(FOUND, 30, PASS) + full(CHOSEN, 30, PASS) + outcomes(BOOKED, *specs))
    assert score.sections[BOOKED].partial is expected_partial


def test_error_counts_toward_partial_the_same_as_skipped():
    score = compute(
        full(FOUND, 30, PASS) + full(CHOSEN, 30, PASS)
        + outcomes(BOOKED, (30, PASS), (10, ERROR))
    )
    assert score.partial
    assert BOOKED in score.partial_sections


def test_partial_booked_refuses_to_segment():
    """The rule that protects the whole call list."""
    score = compute(
        full(FOUND, 30, PASS) + full(CHOSEN, 30, PASS)
        + outcomes(BOOKED, (10, FAIL), (30, SKIPPED))
    )
    assert score.partial
    assert BOOKED in score.partial_sections
    assert score.segment is None, "never segment on incomplete Booked data"
    assert score.segment_priority == UNSEGMENTED_PRIORITY


def test_partial_found_still_segments():
    """Only Booked blocks segmentation. Found being thin does not."""
    score = compute(
        outcomes(FOUND, (23, PASS), (7, SKIPPED))
        + full(CHOSEN, 30, PASS)
        + full(BOOKED, 40, FAIL)
    )
    assert score.partial
    assert FOUND in score.partial_sections
    assert score.segment == SEG_LEAKY_BUCKET


def test_partial_sections_are_named_not_just_flagged():
    score = compute(
        full(FOUND, 30, SKIPPED) + full(CHOSEN, 30, PASS) + full(BOOKED, 40, SKIPPED)
    )
    assert set(score.partial_sections) == {FOUND, BOOKED}


# ── Coverage, which is not partial ────────────────────────────────────────────


def test_coverage_reports_what_we_are_even_attempting():
    """A disabled check shrinks coverage. A skipped check does not."""
    score = compute(
        outcomes(FOUND, (18, PASS))  # SERP and untimestamped checks disabled, basis 18 of 30
        + full(CHOSEN, 30, PASS)
        + full(BOOKED, 40, PASS)
    )
    assert score.sections[FOUND].coverage == pytest.approx(18 / 30)
    assert not score.sections[FOUND].partial, "nothing was skipped, only disabled"
    assert score.total == 100


def test_coverage_of_a_zero_weight_section_is_one():
    from app.scoring import SectionScore

    assert SectionScore(MEASUREMENT, 0, 0, 0, 0).coverage == 1.0


# ── Measurement layer ─────────────────────────────────────────────────────────


def test_measurement_checks_never_touch_the_score():
    baseline = build()
    with_measurement = compute(
        full(FOUND, 30, PASS) + full(CHOSEN, 30, PASS) + full(BOOKED, 40, PASS)
        + outcomes(MEASUREMENT, (0, FAIL), (0, PASS), (0, SKIPPED))
    )
    assert with_measurement.total == baseline.total == 100
    assert not with_measurement.partial


# ── Outcome construction ──────────────────────────────────────────────────────


def test_outcomes_from_skips_disabled_definitions():
    built = outcomes_from({}, CHECK_DEFINITIONS)
    codes = {o.code for o in built}
    assert "F7" in codes
    assert "F8" not in codes, "SERP checks are disabled this week"
    assert len(built) == sum(1 for r in CHECK_DEFINITIONS if r["enabled"])


def test_a_definition_with_no_status_is_skipped_not_absent():
    built = outcomes_from({"F7": PASS}, CHECK_DEFINITIONS)
    by_code = {o.code: o for o in built}
    assert by_code["F7"].status == PASS
    assert by_code["F14"].status == SKIPPED


def test_outcomes_from_carries_points_and_section():
    built = {o.code: o for o in outcomes_from({}, CHECK_DEFINITIONS)}
    assert (built["B1"].points, built["B1"].section) == (10, BOOKED)
    assert (built["F7"].points, built["F7"].section) == (3, FOUND)


def test_a_real_definition_set_scores_end_to_end():
    statuses = {r["code"]: PASS for r in CHECK_DEFINITIONS}
    for code in ("B1", "B2", "B3"):
        statuses[code] = FAIL
    score = compute(outcomes_from(statuses, CHECK_DEFINITIONS))
    assert score.booked == 16
    assert score.booked_max == 40
    assert score.segment == SEG_LEAKY_BUCKET, "high Found, Booked under 20 of 40"
    assert not score.partial


def test_the_canonical_wedge_case_does_not_reach_leaky_bucket():
    """Calibration, not a bug. Worth knowing before the thresholds are trusted.

    The criteria doc describes the wedge as "a roofer spending on ads with no
    booking path and a broken form". That is exactly B1 and B2 failing, and it
    scores Booked 22 of 40, which is over the 20 point Leaky Bucket ceiling. He
    lands in the uncovered middle with no segment at all.

    Reaching Leaky Bucket takes 20 of 40 Booked points gone, and B1 plus B2 is
    only 18. Either the ceiling moves to 22, or the wedge needs a third failure
    to qualify. That is a threshold decision, so it is recorded here rather than
    quietly adjusted.
    """
    statuses = {r["code"]: PASS for r in CHECK_DEFINITIONS}
    statuses["B1"] = FAIL  # no self-serve booking, 10 points
    statuses["B2"] = FAIL  # broken form, 8 points
    score = compute(outcomes_from(statuses, CHECK_DEFINITIONS))
    assert score.booked == 22
    assert score.segment is None


# ── Guards ────────────────────────────────────────────────────────────────────


def test_an_unknown_status_is_rejected_at_construction():
    with pytest.raises(ValueError, match="unknown status"):
        CheckOutcome(code="F1", section=FOUND, status="maybe", points=1)


@pytest.mark.parametrize("status", [PASS, FAIL, SKIPPED, ERROR])
def test_every_valid_status_constructs(status):
    assert CheckOutcome(code="F1", section=FOUND, status=status, points=1).status == status


def test_only_a_pass_earns_points():
    for status in (FAIL, SKIPPED, ERROR):
        assert CheckOutcome(code="F1", section=FOUND, status=status, points=5).earned == 0
    assert CheckOutcome(code="F1", section=FOUND, status=PASS, points=5).earned == 5


def test_normalized_of_an_unknown_section_is_zero():
    assert build().normalized("nonexistent") == 0.0


def test_a_section_with_no_checks_at_all_is_partial():
    """Not merely zero. A section nobody ran is unmeasured, and must say so."""
    score = compute(full(FOUND, 30, PASS))
    chosen = score.sections[CHOSEN]
    assert chosen.basis == 0
    assert chosen.unmeasured_ratio == 1.0
    assert chosen.partial
    assert set(score.partial_sections) == {CHOSEN, BOOKED}
    assert score.segment is None, "Booked was never run"
    assert score.total == 30
