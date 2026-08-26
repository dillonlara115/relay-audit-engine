"""C9, C17, the vision response parser, and the em-dash guard.

No model is called here. The parser is the boundary between a nondeterministic
model and everything downstream, so it is the part that has to be pinned down.
"""

from __future__ import annotations

import pytest

from app.agents.vision import TRUST_VERDICTS, VisionVerdict, parse_verdict
from app.checks import vision  # noqa: F401 - registers the checks
from app.checks.base import REGISTRY, AuditContext
from app.checks.extract import SiteFacts
from app.copy_rules import EM_DASH, EN_DASH, contains_forbidden_dash, sanitize
from app.scoring import FAIL, PASS, SKIPPED


def verdict(**overrides) -> VisionVerdict:
    base = dict(ok=True, stock_photos=False, stock_reason="Crews and finished roofs are shown.",
                trust_verdict="strong", trust_reason="Named crew and a local address.")
    base.update(overrides)
    return VisionVerdict(**base)


def run(code, vision_result):
    ctx = AuditContext(place={}, site=SiteFacts(homepage=None), vision=vision_result)
    return REGISTRY[code](ctx)


# ── Copy rules ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dash", [EM_DASH, EN_DASH, "―", "−"])
def test_every_forbidden_dash_is_detected(dash):
    assert contains_forbidden_dash(f"a {dash} b")


def test_plain_text_is_left_alone():
    text = "Nobody can book a time without waiting for a call back."
    assert sanitize(text) == (text, False)
    assert not contains_forbidden_dash(text)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("real work — not stock", "real work, not stock"),
        ("real work—not stock", "real work, not stock"),
        ("a – b", "a, b"),
        ("one — two — three", "one, two, three"),
    ],
)
def test_dashes_become_commas(raw, expected):
    cleaned, changed = sanitize(raw)
    assert cleaned == expected
    assert changed is True
    assert not contains_forbidden_dash(cleaned)


def test_sanitize_reports_whether_it_changed_anything():
    """Silent cleaning would hide prompt drift, so the flag is recorded."""
    assert sanitize("clean text")[1] is False
    assert sanitize(f"dirty {EM_DASH} text")[1] is True


def test_assert_clean_raises_rather_than_shipping():
    from app.copy_rules import assert_clean

    assert_clean("fine")
    with pytest.raises(ValueError, match="forbidden dash"):
        assert_clean(f"not {EM_DASH} fine", where="report")


def test_sanitize_handles_empty_input():
    assert sanitize(None) == ("", False)
    assert sanitize("") == ("", False)


# ── Parsing the model's answer ────────────────────────────────────────────────


def test_a_good_response_parses():
    result = parse_verdict(
        '{"stock_photos": false, "stock_reason": "Own crews shown.",'
        ' "trust_verdict": "adequate", "trust_reason": "Local address."}'
    )
    assert result.ok
    assert result.stock_photos is False
    assert result.trust_verdict == "adequate"


def test_a_dict_is_accepted_as_well_as_a_string():
    result = parse_verdict({"stock_photos": True, "stock_reason": "x",
                            "trust_verdict": "weak", "trust_reason": "y"})
    assert result.ok and result.stock_photos is True


def test_model_output_is_dash_sanitized_at_the_boundary():
    """CLAUDE.md bans em-dashes in model output, not just in templates.

    The prompt asks, and this catches it when the model does it anyway.
    """
    result = parse_verdict({
        "stock_photos": False,
        "stock_reason": f"Real crews {EM_DASH} not stock imagery.",
        "trust_verdict": "strong",
        "trust_reason": f"Established {EN_DASH} clearly local.",
    })
    assert result.ok
    assert result.sanitized is True
    assert not contains_forbidden_dash(result.stock_reason)
    assert not contains_forbidden_dash(result.trust_reason)
    assert result.stock_reason == "Real crews, not stock imagery."


@pytest.mark.parametrize(
    "payload, why",
    [
        ("not json at all", "unparseable"),
        ("[1,2,3]", "not an object"),
        ('{"stock_photos": "yes", "trust_verdict": "strong"}', "stock_photos is not a bool"),
        ('{"stock_photos": false, "trust_verdict": "excellent"}', "verdict outside the enum"),
        ('{"stock_photos": false}', "verdict missing"),
        ('{"trust_verdict": "strong"}', "stock_photos missing"),
    ],
)
def test_a_drifting_model_produces_a_skip_not_a_sentence(payload, why):
    result = parse_verdict(payload)
    assert result.ok is False, why
    assert result.error


@pytest.mark.parametrize("value", TRUST_VERDICTS)
def test_every_allowed_verdict_parses(value):
    result = parse_verdict({"stock_photos": False, "stock_reason": "",
                            "trust_verdict": value.upper(), "trust_reason": ""})
    assert result.ok and result.trust_verdict == value


def test_to_dict_drops_empties():
    row = verdict(stock_reason="", trust_reason="").to_dict()
    assert "stock_reason" not in row
    assert row["trust_verdict"] == "strong"


# ── Preconditions ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["C9", "C17"])
def test_no_vision_skips(code):
    res = run(code, None)
    assert res.status == SKIPPED
    assert "not looked at" in res.note


@pytest.mark.parametrize("code", ["C9", "C17"])
def test_a_failed_vision_call_skips_rather_than_failing_the_site(code):
    res = run(code, VisionVerdict(ok=False, error="DeadlineExceeded"))
    assert res.status == SKIPPED
    assert res.observed["error"] == "DeadlineExceeded"


# ── C9 project photos ─────────────────────────────────────────────────────────


def test_c9_passes_on_the_companys_own_photographs():
    res = run("C9", verdict(stock_photos=False))
    assert res.status == PASS
    assert "own work" in res.note


def test_c9_fails_on_stock_imagery():
    res = run("C9", verdict(stock_photos=True, stock_reason="Generic catalogue houses."))
    assert res.status == FAIL
    assert "do not look like" in res.note
    assert "Generic catalogue houses." in res.note


def test_c9_carries_the_reason_as_evidence():
    res = run("C9", verdict(stock_photos=True, stock_reason="Watermarked stock."))
    assert res.observed["reason"] == "Watermarked stock."


# ── C17 trust read ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "trust, status",
    [("strong", PASS), ("adequate", PASS), ("weak", FAIL)],
)
def test_c17_wants_adequate_or_better(trust, status):
    res = run("C17", verdict(trust_verdict=trust))
    assert res.status == status
    assert trust in res.note


def test_c17_note_reads_as_a_homeowners_impression():
    res = run("C17", verdict(trust_verdict="weak", trust_reason="No address anywhere."))
    assert "A homeowner would read this as a weak first impression." in res.note
    assert "No address anywhere." in res.note


# ── The prompt itself ─────────────────────────────────────────────────────────


def test_the_prompt_holds_the_lines_that_matter():
    from app.agents.vision import PROMPT

    assert "$15,000 roof replacement" in PROMPT, "the homeowner framing"
    assert "Do not comment on design taste" in PROMPT, "we do not sell web design"
    assert "em-dash" in PROMPT, "the brand rule is asked for as well as enforced"
    assert not contains_forbidden_dash(PROMPT)
