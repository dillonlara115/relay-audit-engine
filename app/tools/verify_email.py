"""Whether an address can receive mail, without sending any.

Three questions, cheapest first: is it shaped like an address, does its domain
publish somewhere to deliver mail, and is it the kind of mailbox a person
reads. No message is transmitted at any point, so this sits outside the
outreach path that hard rule 4 governs. It tells an operator which of the
addresses discovery found is worth hand-writing to.

The fourth status matters as much as the other three. A DNS timeout is
UNKNOWN, never INVALID: we did not measure it, and treating an unmeasured
address as dead would quietly delete a real prospect. Same contract as a
skipped check.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.tools.contacts import KIND_ROLE, _kind_of, normalize

VALID = "valid"
RISKY = "risky"
INVALID = "invalid"
UNKNOWN = "unknown"

VALID_STATUSES = frozenset({VALID, RISKY, INVALID, UNKNOWN})

DNS_TIMEOUT_SECONDS = 5.0

# Throwaway inbox providers. An address here reaches nobody in a week.
_DISPOSABLE = frozenset({
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "getnada.com", "sharklasers.com", "dispostable.com", "maildrop.cc",
})

# Free consumer mail. Deliverable and common for a small roofing outfit, but it
# is not the company's own domain, so a human should look before writing.
_FREEMAIL = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "live.com", "msn.com", "comcast.net", "att.net",
    "verizon.net", "sbcglobal.net", "cox.net", "earthlink.net", "protonmail.com",
})


@dataclass(frozen=True)
class Verdict:
    """What we learned, and the one-line reason an operator reads."""

    email: str
    status: str
    reason: str

    @property
    def usable(self) -> bool:
        """Worth putting in front of a person. RISKY still is, with a caveat."""
        return self.status in (VALID, RISKY)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason}


def _resolver() -> Any | None:
    """dnspython if it is installed, else None.

    Kept optional on purpose. Discovery is useful on its own, and a missing
    resolver should downgrade every verdict to UNKNOWN rather than break a
    sweep.
    """
    try:
        import dns.resolver  # noqa: PLC0415
    except ImportError:
        return None
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    return resolver


@lru_cache(maxsize=2048)
def domain_accepts_mail(domain: str) -> bool | None:
    """True with an MX, False with neither MX nor A, None when we could not ask.

    A domain with an A record but no MX is still deliverable under the RFC
    fallback, and plenty of small contractors are configured exactly that way,
    so it counts as accepting mail.
    """
    resolver = _resolver()
    if resolver is None:
        return None

    import dns.resolver  # noqa: PLC0415

    try:
        answers = resolver.resolve(domain, "MX")
        if len(answers) > 0:
            return True
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        pass
    except dns.resolver.NXDOMAIN:
        return False
    except Exception:
        return None

    try:
        resolver.resolve(domain, "A")
        return True
    except dns.resolver.NXDOMAIN:
        return False
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False
    except Exception:
        return None


def verify(raw: str | None) -> Verdict:
    """One address in, one verdict out. Never raises, never sends."""
    email = normalize(raw)
    if not email:
        return Verdict(str(raw or ""), INVALID, "Not a usable address.")

    domain = email.split("@", 1)[1]

    if domain in _DISPOSABLE:
        return Verdict(email, INVALID, "Throwaway mailbox provider.")

    accepts = domain_accepts_mail(domain)
    if accepts is False:
        return Verdict(email, INVALID, "Domain does not accept mail.")
    if accepts is None:
        return Verdict(email, UNKNOWN, "Could not check the domain.")

    if domain in _FREEMAIL:
        return Verdict(email, RISKY, "Personal mailbox, not the company domain.")
    if _kind_of(email) == KIND_ROLE:
        return Verdict(email, RISKY, "Shared mailbox, so no named reader.")
    return Verdict(email, VALID, "Domain accepts mail.")


def verify_all(emails: list[str]) -> list[Verdict]:
    return [verify(e) for e in emails]
