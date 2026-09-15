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


# ── grounding the draft in what passed ────────────────────────────────────────
#
# Triton Roofing, batch 2MqjsKpPqeiBw5FhFlrA: C17 is read off a screenshot and
# reported "the page lacks visible trust signals such as a physical address,
# local phone number, reviews, or credentials". On the same audit C5, C10 and
# C11 had confirmed the phone is visible, reviews are on the homepage, and the
# site states it is licensed and insured. Given only the failures, the draft
# turned the screenshot's guess into a finding telling the owner his site shows
# none of the three. A human caught it at the approval gate. The passing checks
# go into the prompt so the next one does not get that far.


def test_the_prompt_states_what_passed_as_ground_truth():
    from app.agents.diagnostician import PROMPT, _passing_block

    block = _passing_block([
        {"code": "C5", "title": "Phone above fold",
         "note": "The phone number (719) 322-3673 is visible without scrolling."},
        {"code": "C10", "title": "Reviews on page",
         "note": "Customer reviews appear on the homepage."},
    ])
    assert "(719) 322-3673 is visible without scrolling" in block
    assert "Customer reviews appear on the homepage" in block
    # A finding may only cite a failed check, so passing codes stay out of it.
    assert "C5" not in block and "C10" not in block

    filled = PROMPT.format(business_name="Triton Roofing", city="Colorado Springs",
                           count=6, passing=block,
                           failures="- C17 (Trust read, 2 pts): weak")
    assert "Never say any of these is missing" in filled
    assert "(719) 322-3673" in filled


def test_an_audit_with_nothing_passing_still_renders():
    from app.agents.diagnostician import _passing_block

    assert _passing_block([]) == "(nothing on this site was confirmed working)"


def test_a_passing_check_with_no_note_still_lists_its_title():
    from app.agents.diagnostician import _passing_block

    assert _passing_block([{"code": "C11", "title": "Licensed / insured"}]) == (
        "- Licensed / insured")
