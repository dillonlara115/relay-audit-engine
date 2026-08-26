"""The diagnostician's parse boundary. Rules enforced by code, not prompt."""

from __future__ import annotations

import json

import pytest

from app.agents.diagnostician import Diagnosis, parse_diagnosis
from app.copy_rules import EM_DASH, contains_forbidden_dash

CODES = ["B1", "B2", "C5", "F7", "C15"]


def finding(code="B1", **overrides):
    base = {
        "check_code": code,
        "what_we_saw": "Nobody can book a time without waiting for a call back.",
        "what_it_means": "Homeowners who want an answer now move to whoever gives one.",
        "what_fixing_takes": "A service his office can turn on in an afternoon.",
    }
    base.update(overrides)
    return base


def draft(*findings):
    return json.dumps({"findings": list(findings)})


def test_a_clean_draft_of_three_parses():
    d = parse_diagnosis(draft(finding("B1"), finding("B2"), finding("C5")), valid_codes=CODES)
    assert d.ok
    assert [f.check_code for f in d.findings] == ["B1", "B2", "C5"]
    assert [f.ordinal for f in d.findings] == [1, 2, 3]
    assert not d.needs_review


@pytest.mark.parametrize("count", [0, 1, 2, 4, 5])
def test_exactly_three_is_a_rule_not_a_request(count):
    rows = [finding(CODES[i % len(CODES)]) for i in range(count)]
    d = parse_diagnosis(draft(*rows), valid_codes=CODES)
    assert not d.ok
    assert "expected 3" in d.error


def test_a_finding_must_cite_a_check_that_actually_failed():
    """A finding about a check that passed is an invented problem."""
    d = parse_diagnosis(draft(finding("B1"), finding("B2"), finding("C3")), valid_codes=CODES)
    assert not d.ok
    assert "did not fail" in d.error


def test_duplicate_codes_are_rejected():
    d = parse_diagnosis(draft(finding("B1"), finding("B1"), finding("B2")), valid_codes=CODES)
    assert not d.ok


def test_dashes_are_sanitized_and_recorded():
    dirty = finding("B1", what_it_means=f"Jobs go elsewhere {EM_DASH} quickly.")
    d = parse_diagnosis(draft(dirty, finding("B2"), finding("C5")), valid_codes=CODES)
    assert d.ok
    assert d.findings[0].sanitized
    assert not contains_forbidden_dash(d.findings[0].what_it_means)


def test_mechanism_language_flags_for_the_approving_human():
    """The model drafts, the human approves. A draft that says 'schema' is not
    rejected, it arrives flagged so the human cannot miss it."""
    leaky = finding("F7", what_we_saw="The phone number in the schema markup does not match.")
    d = parse_diagnosis(draft(leaky, finding("B2"), finding("C5")), valid_codes=CODES)
    assert d.ok
    assert d.needs_review
    assert "schema" in d.findings[0].mechanism_flags


def test_score_language_flags_too():
    leaky = finding("B1", what_it_means="His Booked score drops 10 points.")
    d = parse_diagnosis(draft(leaky, finding("B2"), finding("C5")), valid_codes=CODES)
    assert d.ok and d.needs_review


def test_empty_fields_are_rejected():
    d = parse_diagnosis(draft(finding("B1", what_it_means="  "), finding("B2"), finding("C5")),
                        valid_codes=CODES)
    assert not d.ok


@pytest.mark.parametrize("garbage", ["not json", "[1,2]", '{"findings": "three"}'])
def test_a_drifting_model_is_an_error_not_a_report(garbage):
    assert not parse_diagnosis(garbage, valid_codes=CODES).ok
