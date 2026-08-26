"""The public report payload. What a contractor sees, and nothing else.

This DTO is the privacy boundary. It carries no scores, no bands, no segments,
no check codes, and a test walks its serialized form to prove that. Everything
in it is either his own business information or a finding a human approved.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

CALCULATOR_URL = "https://relayforroofers.com/tools/lead-leakage-calculator/"

FINDINGS_REQUIRED = 3

# Words that must never appear in a serialized public payload. Matched on word
# boundaries: a finding that says a homeowner "abandons" the site is fine, a
# payload that says "band" is not. Verified against a real page where the
# substring version false-flagged exactly that word.
FORBIDDEN_PAYLOAD_TERMS = ("score", "band", "segment", "leaky", "dialed",
                           "invisible pro", "both broken", "partial")


def forbidden_terms_in(text: str) -> list[str]:
    """The internal vocabulary found in a public artifact, word-bounded."""
    import re

    lowered = text.lower()
    return [t for t in FORBIDDEN_PAYLOAD_TERMS
            if re.search(rf"\b{re.escape(t)}\b", lowered)]


@dataclass(frozen=True)
class PublicFinding:
    ordinal: int
    what_we_saw: str
    what_it_means: str
    what_fixing_takes: str


@dataclass(frozen=True)
class PublicReport:
    slug: str
    business_name: str
    city: str
    findings: tuple[PublicFinding, ...]
    screenshot_url: str | None = None
    calculator_url: str = CALCULATOR_URL
    competitor_note: str | None = None   # optional, human-written, named only if fair

    def __post_init__(self) -> None:
        # Exactly three, enforced at runtime, not just at publish time.
        if len(self.findings) != FINDINGS_REQUIRED:
            raise ValueError(
                f"a report carries exactly {FINDINGS_REQUIRED} findings, got {len(self.findings)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "business_name": self.business_name,
            "city": self.city,
            "findings": [
                {
                    "ordinal": f.ordinal,
                    "what_we_saw": f.what_we_saw,
                    "what_it_means": f.what_it_means,
                    "what_fixing_takes": f.what_fixing_takes,
                }
                for f in self.findings
            ],
            "screenshot_url": self.screenshot_url,
            "calculator_url": self.calculator_url,
            "competitor_note": self.competitor_note,
        }


def new_slug() -> str:
    """16 URL-safe characters, unguessable. The only access control the public
    page has, so it comes from the CSPRNG."""
    return secrets.token_urlsafe(12)


def hash_ip(raw_ip: str, salt: str) -> str:
    """Guardrail 5: never store a raw IP. The hash is enough to count distinct
    readers, and nothing reverses it without the server-side salt."""
    if not salt:
        raise ValueError("REPORT_IP_SALT is not configured")
    return hashlib.sha256(f"{salt}:{raw_ip}".encode()).hexdigest()
