"""Check definitions. These are the contract the scoring module multiplies by."""

from __future__ import annotations

import pytest

from app.checks import definitions as defs
from app.checks.definitions import (
    BOOKED,
    CHECK_DEFINITIONS,
    CHOSEN,
    FOUND,
    MEASUREMENT,
    SECTION_WEIGHTS,
    by_code,
    section_points,
)


def test_the_criteria_doc_has_forty_scored_checks():
    scored = [r for r in CHECK_DEFINITIONS if r["section"] != MEASUREMENT]
    assert len(scored) == 40
    assert len([r for r in CHECK_DEFINITIONS if r["section"] == MEASUREMENT]) == 4


@pytest.mark.parametrize("section, weight", [(FOUND, 30), (CHOSEN, 30), (BOOKED, 40)])
def test_section_points_sum_to_the_nominal_weight(section, weight):
    assert section_points(section) == weight
    assert SECTION_WEIGHTS[section] == weight


def test_measurement_checks_are_worth_nothing():
    assert section_points(MEASUREMENT) == 0
    assert all(r["points"] == 0 for r in CHECK_DEFINITIONS if r["section"] == MEASUREMENT)


def test_codes_are_unique():
    codes = [r["code"] for r in CHECK_DEFINITIONS]
    assert len(codes) == len(set(codes))


def test_by_code_indexes_every_definition():
    index = by_code()
    assert len(index) == len(CHECK_DEFINITIONS)
    assert index["B1"]["points"] == 10
    assert index["F7"]["title"] == "Phone match"


def test_enabled_only_excludes_the_checks_we_are_not_running():
    assert section_points(FOUND, enabled_only=True) == 18
    assert section_points(CHOSEN, enabled_only=True) == 28  # C9 cut
    assert section_points(BOOKED, enabled_only=True) == 36  # B6 cut, see its disabled_reason


def test_every_disabled_check_says_why():
    for row in CHECK_DEFINITIONS:
        if not row["enabled"]:
            assert row.get("disabled_reason"), f"{row['code']} is off with no reason given"


def test_only_the_unmeasurable_booked_check_is_disabled():
    """Booked carries the most weight and is the section nobody else audits.
    B6 is off because measuring it requires submitting the form, which hard
    rule 1 forbids absolutely. Anything else going dark here needs a reason
    as strong as that one."""
    disabled = [r["code"] for r in CHECK_DEFINITIONS
                if r["section"] == BOOKED and not r["enabled"]]
    assert disabled == ["B6"]


def test_every_definition_carries_the_fields_firestore_expects():
    required = {"code", "section", "title", "full_credit", "points", "source", "automation",
                "sort_order", "enabled"}
    for row in CHECK_DEFINITIONS:
        assert required <= set(row), f"{row['code']} is missing {required - set(row)}"


def test_sort_order_is_unique_and_ascending_within_a_section():
    orders = [r["sort_order"] for r in CHECK_DEFINITIONS]
    assert len(orders) == len(set(orders))
    assert orders == sorted(orders)


# ── The import-time guard ─────────────────────────────────────────────────────


def test_a_reweighted_section_fails_at_import(monkeypatch):
    """A typo in a points value silently reweights every audit. It must not."""
    broken = [dict(r) for r in CHECK_DEFINITIONS]
    broken[1]["points"] = 99
    monkeypatch.setattr(defs, "CHECK_DEFINITIONS", broken)
    with pytest.raises(AssertionError, match="check points sum to"):
        defs._assert_weights()


def test_a_duplicate_code_fails_at_import(monkeypatch):
    duplicated = [dict(r) for r in CHECK_DEFINITIONS]
    duplicated[1] = dict(duplicated[1], code=duplicated[0]["code"])
    monkeypatch.setattr(defs, "CHECK_DEFINITIONS", duplicated)
    with pytest.raises(AssertionError, match="duplicate check code"):
        defs._assert_weights()
