"""Phone normalization. Shared by the fit gate and, later, check F7.

Comparing a site phone to a GBP phone is only meaningful if both sides are
reduced to the same canonical form first. Extensions are stripped for the
comparison but preserved on the parsed record, because a tracking number swap
and a suite extension look identical if you throw the extension away silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers

DEFAULT_REGION = "US"

_TEL_HREF = re.compile(r"""tel:\s*([+0-9().\-\s;,extEXT#*]+)""", re.I)
_LOOSE = re.compile(
    r"""(?:\+?1[\s.\-]?)?          # optional country code
        \(?\d{3}\)?[\s.\-]?        # area code
        \d{3}[\s.\-]?\d{4}         # subscriber
        (?:\s*(?:x|ext\.?|extension|\#)\s*\d{1,6})?  # \# because re.X treats bare # as a comment
        """,
    re.I | re.X,
)

# Colorado Front Range and Western US area codes we treat as "local enough"
# for the gate's local-operator check. Extend per market as we add metros.
FRONT_RANGE_AREA_CODES = frozenset({"303", "719", "720", "970", "983"})


@dataclass(frozen=True)
class Phone:
    e164: str
    national: str
    area_code: str
    extension: str | None = None

    def __str__(self) -> str:
        return self.e164


def parse_phone(raw: str | None, region: str = DEFAULT_REGION) -> Phone | None:
    """Return a canonical Phone, or None when the input is not a real number.

    Never guesses. A string that does not parse to a valid number is None, not
    a best-effort digit soup.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if text.lower().startswith("tel:"):
        text = text[4:]

    extension: str | None = None
    ext_match = re.search(r"(?:x|ext\.?|extension|#|;ext=)\s*(\d{1,6})\s*$", text, re.I)
    if ext_match:
        extension = ext_match.group(1)
        text = text[: ext_match.start()]

    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    national = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    digits = str(parsed.national_number)
    area_code = digits[:3] if len(digits) == 10 else ""
    return Phone(e164=e164, national=national, area_code=area_code, extension=extension)


def same_number(a: str | None, b: str | None, region: str = DEFAULT_REGION) -> bool:
    """True only when both parse and share an E.164 form. Unknown is not equal."""
    left, right = parse_phone(a, region), parse_phone(b, region)
    if left is None or right is None:
        return False
    return left.e164 == right.e164


def extract_phones(html: str | None, text: str | None = None) -> list[Phone]:
    """Phones found in tel: hrefs first, then loose matches in visible text."""
    found: list[Phone] = []
    seen: set[str] = set()

    for source, pattern in ((html or "", _TEL_HREF), (text or "", _LOOSE)):
        if not source:
            continue
        for match in pattern.finditer(source):
            candidate = match.group(1) if pattern is _TEL_HREF else match.group(0)
            phone = parse_phone(candidate)
            if phone and phone.e164 not in seen:
                seen.add(phone.e164)
                found.append(phone)
    return found
