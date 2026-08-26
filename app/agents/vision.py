"""The vision component. One of only four things in this system that get a model.

It looks at a mobile screenshot of a homepage and answers two questions a
homeowner answers in about three seconds: are these real photographs of real
work, and does this look like a company worth a fifteen thousand dollar job.

Structured JSON only, never prose, per the engine spec. The output is validated,
dash-sanitized and clamped to the allowed verdicts before anything is stored,
because a model that drifts should produce a skipped check rather than a
sentence that ends up in front of a contractor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import get_config
from app.copy_rules import sanitize

TRUST_VERDICTS = ("strong", "adequate", "weak")

# Measured at 102 to 108 tokens for a real answer with reasoning disabled.
MAX_OUTPUT_TOKENS = 600

# Verbatim from the engine spec, plus the two house rules. The homeowner framing
# and the "do not comment on design taste" line are the load bearing parts: a
# model asked to look at a website will grade the web design otherwise, and web
# design is not what we are selling.
PROMPT = """Given this mobile screenshot of a roofing contractor's homepage, answer as JSON:
{"stock_photos": bool, "stock_reason": str,
 "trust_verdict": "strong"|"adequate"|"weak", "trust_reason": str}

Answer as a homeowner deciding who to trust with a $15,000 roof replacement.
Do not comment on design taste. Comment on whether this looks like a real,
established local company.

stock_photos is true when the imagery looks like purchased stock photography or
generic renders rather than photographs of this company's own crews, trucks and
finished roofs. If there are no meaningful photographs at all, that is true too,
and say so in stock_reason.

Keep each reason to one sentence. Never use an em-dash or an en-dash."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "stock_photos": {"type": "BOOLEAN"},
        "stock_reason": {"type": "STRING"},
        "trust_verdict": {"type": "STRING", "enum": list(TRUST_VERDICTS)},
        "trust_reason": {"type": "STRING"},
    },
    "required": ["stock_photos", "stock_reason", "trust_verdict", "trust_reason"],
}


@dataclass(frozen=True)
class VisionVerdict:
    ok: bool
    stock_photos: bool | None = None
    stock_reason: str = ""
    trust_verdict: str | None = None
    trust_reason: str = ""
    model: str | None = None
    sanitized: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", False)}


def parse_verdict(raw: Any, *, model: str | None = None) -> VisionVerdict:
    """Validate and clean the model's answer. Anything unusable is not ok."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            return VisionVerdict(ok=False, model=model, error=f"unparseable JSON: {exc}")
    if not isinstance(raw, dict):
        return VisionVerdict(ok=False, model=model, error="response was not an object")

    stock = raw.get("stock_photos")
    verdict = raw.get("trust_verdict")
    if not isinstance(stock, bool):
        return VisionVerdict(ok=False, model=model, error=f"stock_photos was {stock!r}")
    if not isinstance(verdict, str) or verdict.strip().lower() not in TRUST_VERDICTS:
        return VisionVerdict(ok=False, model=model, error=f"trust_verdict was {verdict!r}")

    stock_reason, dirty_a = sanitize(str(raw.get("stock_reason") or ""))
    trust_reason, dirty_b = sanitize(str(raw.get("trust_reason") or ""))

    return VisionVerdict(
        ok=True,
        stock_photos=stock,
        stock_reason=stock_reason,
        trust_verdict=verdict.strip().lower(),
        trust_reason=trust_reason,
        model=model,
        sanitized=dirty_a or dirty_b,
    )


async def read_screenshot(image: bytes, *, mime_type: str = "image/png") -> VisionVerdict:
    """Ask the model to look at one homepage. Never raises."""
    cfg = get_config()
    if not image:
        return VisionVerdict(ok=False, error="no screenshot to read")

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            vertexai=cfg.use_vertexai, project=cfg.project, location=cfg.model_location
        )
        response = await client.aio.models.generate_content(
            model=cfg.gemini_model,
            contents=[
                types.Part.from_bytes(data=image, mime_type=mime_type),
                PROMPT,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                # The same screenshot should produce the same verdict twice.
                temperature=0.0,
                # max_output_tokens covers reasoning as well as the answer, and
                # this model reasons by default. Measured on a real homepage it
                # spent 380 of a 400 token budget thinking and then emitted a
                # truncated five token object. Turning reasoning off produced an
                # identical verdict for a seventh of the tokens, which is the
                # right trade for a structured read of one image.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a model fault skips its checks
        return VisionVerdict(ok=False, model=cfg.gemini_model,
                             error=f"{type(exc).__name__}: {exc}"[:300])

    # A truncated response is unparseable JSON, and "unparseable JSON" does not
    # tell you the budget was too small. Name the reason.
    finish = getattr((response.candidates or [None])[0], "finish_reason", None)
    if finish is not None and str(finish).endswith("MAX_TOKENS"):
        return VisionVerdict(ok=False, model=cfg.gemini_model,
                             error=f"response truncated at {MAX_OUTPUT_TOKENS} tokens")

    return parse_verdict(response.text, model=cfg.gemini_model)
