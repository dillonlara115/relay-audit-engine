"""Reading a reply and deciding what kind of answer it is.

The fourth and last component that gets a model, after the coordinator, the
vision read and the diagnostician. Everything else in the pipeline is a plain
function, and this is here only because sorting "not this quarter, try me in
the spring" from "take me off your list" is a language judgement.

What it returns is an intent, never an action. `app/outreach.POLICIES` decides
what an intent does to a sequence, and that table is a business decision the
model has no business making. Keeping the two apart is also what lets an
operator correct a classification without the model having already suppressed
somebody.

Low confidence resolves to OTHER, which parks the sequence for a human rather
than guessing. A reply we cannot read is the one case where carrying on is
certainly wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import get_config
from app.outreach import (
    INTERESTED,
    INTENTS,
    MAYBE_LATER,
    NOT_INTERESTED,
    OTHER,
    OUT_OF_OFFICE,
    WRONG_PERSON,
)

# Below this, we do not trust the label enough to act on it.
CONFIDENCE_FLOOR = 0.6

PROMPT = """A roofing contractor replied to a cold email about problems on his website.
Classify what kind of reply it is. Nothing else.

interested       He wants to talk, see more, get a call, or asks a real question about
                 the work. Any forward motion counts.
maybe_later      Not now but not never. Names a future time, a budget cycle, a season,
                 or says he is busy and to try again.
wrong_person     He is not the decision maker. Forwarding it, passing it on, or naming
                 somebody else to talk to.
not_interested   No. Includes take me off your list, stop emailing, we already have
                 someone, and anything hostile.
out_of_office    An automatic reply. Vacation, away, out of the office, a bounce
                 notice, or anything plainly machine generated.
other            A person wrote something that fits none of the above, or the message
                 is too unclear to place.

Judge only what he wrote. Do not infer enthusiasm from politeness: "thanks, I will take
a look" with no commitment is maybe_later, not interested.

Confidence is how sure you are, from 0 to 1. Be honest and use the low end. A short or
ambiguous reply should score low even when one label seems likeliest.

Reply:
\"\"\"
{body}
\"\"\"
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": list(INTENTS)},
        "confidence": {"type": "NUMBER"},
        "reason": {"type": "STRING"},
    },
    "required": ["intent", "confidence", "reason"],
}

# Unambiguous auto-responder markers. Checked before the model runs, because an
# out of office is a fixed phrase and paying for a model call to read one is
# waste. Never used to claim a human intent, only the machine one.
_AUTO_REPLY_MARKERS = (
    "out of office", "out-of-office", "automatic reply", "auto-reply",
    "autoreply", "away from my desk", "on vacation", "annual leave",
    "i am currently away", "i'm currently away", "undeliverable",
    "delivery status notification", "mail delivery subsystem",
)


@dataclass(frozen=True)
class Classification:
    intent: str
    confidence: float
    reason: str
    model: str | None = None
    auto: bool = False          # matched a marker, no model call was made
    error: str | None = None

    @property
    def trusted(self) -> bool:
        return self.error is None and self.confidence >= CONFIDENCE_FLOOR

    @property
    def effective_intent(self) -> str:
        """What the ledger should act on. Anything untrusted parks for a human."""
        return self.intent if self.trusted else OTHER

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "intent": self.intent,
            "effective_intent": self.effective_intent,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            "auto": self.auto,
        }
        if self.model:
            row["model"] = self.model
        if self.error:
            row["error"] = self.error
        return row


def detect_auto_reply(subject: str, body: str) -> bool:
    blob = f"{subject or ''} {body or ''}".lower()
    return any(marker in blob for marker in _AUTO_REPLY_MARKERS)


def parse_classification(raw: Any, *, model: str | None = None) -> Classification:
    """Validate the model's answer. An unusable one is OTHER, not a crash."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            return Classification(OTHER, 0.0, "", model=model,
                                  error=f"unparseable JSON: {exc}")
    if not isinstance(raw, dict):
        return Classification(OTHER, 0.0, "", model=model, error="response was not an object")

    intent = str(raw.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        return Classification(OTHER, 0.0, "", model=model,
                              error=f"unknown intent {intent!r}")
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return Classification(OTHER, 0.0, "", model=model, error="confidence was not a number")

    return Classification(
        intent=intent,
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(raw.get("reason") or "").strip()[:300],
        model=model,
    )


async def classify(body: str, *, subject: str = "") -> Classification:
    """One reply in, one intent out. Never raises."""
    text = (body or "").strip()
    if not text:
        return Classification(OTHER, 0.0, "The reply had no readable text.")

    if detect_auto_reply(subject, text):
        return Classification(OUT_OF_OFFICE, 1.0, "Automatic reply.", auto=True)

    cfg = get_config()
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(vertexai=cfg.use_vertexai, project=cfg.project,
                              location=cfg.model_location)
        response = await client.aio.models.generate_content(
            model=cfg.gemini_model,
            contents=PROMPT.format(body=text[:4000]),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=300,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a model fault parks the reply
        return Classification(OTHER, 0.0, "", model=cfg.gemini_model,
                              error=f"{type(exc).__name__}: {exc}"[:300])

    return parse_classification(response.text, model=cfg.gemini_model)
