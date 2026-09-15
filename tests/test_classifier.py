"""Reply intent classification. The model is stubbed; the contract is not.

What matters here is that an unreadable answer becomes OTHER, which parks the
sequence for a human, rather than a confident guess that closes or suppresses.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.classifier import (
    CONFIDENCE_FLOOR,
    Classification,
    detect_auto_reply,
    parse_classification,
)
from app.outreach import INTENTS, INTERESTED, NOT_INTERESTED, OTHER, OUT_OF_OFFICE


def result(intent=INTERESTED, confidence=0.9, reason="wants a call"):
    return parse_classification({"intent": intent, "confidence": confidence, "reason": reason})


def test_a_confident_label_is_trusted():
    got = result()
    assert got.trusted is True
    assert got.effective_intent == INTERESTED


def test_a_label_below_the_floor_parks_for_a_human():
    got = result(NOT_INTERESTED, confidence=CONFIDENCE_FLOOR - 0.01)
    assert got.trusted is False
    assert got.effective_intent == OTHER
    # The model's own guess is kept for the operator to see and override.
    assert got.intent == NOT_INTERESTED


def test_an_unparseable_answer_never_suppresses_anybody():
    """The failure that matters. A crashed classifier must not read as a no."""
    for raw in ("not json", {"intent": "nonsense", "confidence": 1}, [], None,
                {"intent": NOT_INTERESTED, "confidence": "very"}):
        assert parse_classification(raw).effective_intent == OTHER


def test_confidence_outside_zero_to_one_is_clamped():
    assert parse_classification({"intent": INTERESTED, "confidence": 4, "reason": ""}).confidence == 1.0
    assert parse_classification({"intent": INTERESTED, "confidence": -2, "reason": ""}).confidence == 0.0


def test_every_intent_the_schema_offers_is_one_the_ledger_knows():
    from app.agents.classifier import RESPONSE_SCHEMA

    assert set(RESPONSE_SCHEMA["properties"]["intent"]["enum"]) == set(INTENTS)


@pytest.mark.parametrize("subject,body", [
    ("Automatic reply: Out of office", ""),
    ("Re: a quick thought", "I am currently away until the 20th."),
    ("Undeliverable: a quick thought", ""),
    ("", "Mail Delivery Subsystem"),
])
def test_an_auto_responder_is_caught_without_a_model_call(subject, body):
    assert detect_auto_reply(subject, body) is True


def test_a_real_reply_is_not_mistaken_for_an_auto_responder():
    assert detect_auto_reply("Re: a quick thought", "Call me Thursday, I am around.") is False


def test_an_auto_reply_shortcut_returns_out_of_office_with_no_model():
    from app.agents.classifier import classify

    got = asyncio.run(classify("I am currently away on vacation.", subject="Automatic reply"))
    assert got.intent == OUT_OF_OFFICE
    assert got.auto is True
    assert got.model is None


def test_an_empty_reply_parks_rather_than_guessing():
    from app.agents.classifier import classify

    assert asyncio.run(classify("   ")).effective_intent == OTHER


def test_the_stored_shape_keeps_both_the_guess_and_what_was_acted_on():
    """An operator correcting a label needs to see what the model actually said."""
    row = result(NOT_INTERESTED, confidence=0.2).to_dict()
    assert row["intent"] == NOT_INTERESTED
    assert row["effective_intent"] == OTHER
    assert row["confidence"] == 0.2


def test_the_classifier_returns_an_intent_and_never_an_action():
    """No field here decides anything. POLICIES does that."""
    row = result().to_dict()
    for forbidden in ("suppress", "close", "policy", "action"):
        assert forbidden not in row
