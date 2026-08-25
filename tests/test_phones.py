"""Phone normalization. Engine spec section 10 asks for 20+ real-world variants.

F7 compares a site phone to a GBP phone, and that comparison is only meaningful
if both sides reduce to the same canonical form. These are the shapes roofing
sites actually publish.
"""

from __future__ import annotations

import pytest

from app.tools.phones import extract_phones, parse_phone, same_number

E164 = "+17195550142"

VARIANTS = [
    "7195550142",
    "719 555 0142",
    "719-555-0142",
    "719.555.0142",
    "(719) 555-0142",
    "(719)555-0142",
    "(719) 555 0142",
    "+1 719 555 0142",
    "+17195550142",
    "1-719-555-0142",
    "1 (719) 555-0142",
    "+1 (719) 555-0142",
    "tel:+17195550142",
    "tel:7195550142",
    "  (719) 555-0142  ",
    "719-555-0142 ext 12",
    "719-555-0142 x12",
    "719.555.0142 ext. 12",
    "(719) 555-0142 extension 12",
    "719-555-0142 #12",
    "+1-719-555-0142;ext=12",
    "CALL (719) 555-0142".replace("CALL ", ""),
]


@pytest.mark.parametrize("raw", VARIANTS)
def test_every_variant_normalizes_to_one_e164(raw):
    parsed = parse_phone(raw)
    assert parsed is not None, raw
    assert parsed.e164 == E164
    assert parsed.area_code == "719"


@pytest.mark.parametrize(
    "raw, extension",
    [
        ("719-555-0142 ext 12", "12"),
        ("719-555-0142 x12", "12"),
        ("719.555.0142 ext. 4501", "4501"),
        ("(719) 555-0142 extension 7", "7"),
        ("719-555-0142 #12", "12"),
        ("(719) 555-0142", None),
    ],
)
def test_extension_is_preserved_not_discarded(raw, extension):
    """A suite extension and a tracking-number swap look identical if you
    silently drop the extension, so it stays on the record."""
    parsed = parse_phone(raw)
    assert parsed is not None
    assert parsed.extension == extension


@pytest.mark.parametrize(
    "raw",
    ["", None, "not a phone", "555-0142", "123", "719-555-014", "000-000-0000"],
)
def test_unparseable_input_is_none_never_a_guess(raw):
    assert parse_phone(raw) is None


def test_vanity_letters_resolve_to_the_number_they_dial():
    """A site printing CALL-NOW and a profile printing 225-5669 are one line.

    F7 would otherwise report a phone mismatch that does not exist.
    """
    assert parse_phone("(719) CALL-NOW").e164 == "+17192255669"
    assert same_number("(719) CALL-NOW", "719-225-5669")


def test_same_number_matches_across_formats():
    assert same_number("(719) 555-0142", "+1 719 555 0142")
    assert same_number("7195550142", "tel:+17195550142")


def test_same_number_is_false_when_either_side_is_unknown():
    """Guardrail 4. Absent is not equal, and it is not a match."""
    assert not same_number(None, "(719) 555-0142")
    assert not same_number("(719) 555-0142", "")
    assert not same_number(None, None)


def test_different_numbers_do_not_match():
    assert not same_number("(719) 555-0142", "(719) 555-0143")


def test_a_call_tracking_swap_is_visible():
    """The exact F7 finding: the site publishes a number the profile does not."""
    assert not same_number("(719) 555-0142", "(844) 555-9900")


def test_extract_prefers_tel_hrefs_then_visible_text():
    html = '<a href="tel:+17195550142">Call us</a><p>or 719-555-0199</p>'
    text = "call us or 719-555-0199"
    found = extract_phones(html, text)
    assert [p.e164 for p in found] == ["+17195550142", "+17195550199"]


def test_extract_dedupes_the_same_number_in_both_places():
    html = '<a href="tel:7195550142">(719) 555-0142</a>'
    found = extract_phones(html, "(719) 555-0142")
    assert [p.e164 for p in found] == [E164]


def test_extract_on_empty_input_is_empty():
    assert extract_phones(None, None) == []
    assert extract_phones("", "") == []
