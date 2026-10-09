"""Addresses for a prospect's domain from Hunter (hunter.io).

The crawl only sees what a roofer put on their own site, and three in four
put no address there at all. Hunter indexes addresses published anywhere on
the web for a domain: a Facebook page, a directory, a permit filing. One
domain search costs one search credit, so searches run only when a person
asks for them, and stop at HUNTER_MONTHLY_CAP.

Nothing here contacts anybody. Every address Hunter returns is checked again
by verify_email, the same as an address found on the site, and Hunter's own
verdict can only lower that: an address Hunter knows to be dead is INVALID,
and one on a domain that accepts every address is RISKY, because nothing
could confirm a real mailbox behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from app.config import get_config
from app.tools.contacts import KIND_PERSONAL, KIND_ROLE, normalize
from app.tools.verify_email import INVALID, RISKY, VALID, verify

BASE = "https://api.hunter.io/v2"
TIMEOUT = 20.0
SOURCE = "hunter"


class HunterUnavailable(RuntimeError):
    """No key, a refused key, or no searches left. Said plainly to the operator."""


@dataclass(frozen=True)
class Found:
    email: str
    personal: bool
    name: str = ""
    position: str = ""
    confidence: int = 0
    hunter_status: str = ""   # valid | accept_all | unknown | invalid | ""
    sources: int = 0


def _key() -> str:
    key = get_config().hunter_api_key
    if not key:
        raise HunterUnavailable("HUNTER_API_KEY is not set. Create a key under hunter.io, API, "
                                "and put it in Secret Manager.")
    return key


def _get(path: str, params: Mapping[str, Any], *, client: httpx.Client | None = None) -> dict[str, Any]:
    own = client is None
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        r = http.get(f"{BASE}{path}", params={**params, "api_key": _key()})
    except httpx.HTTPError as exc:
        raise HunterUnavailable(f"Could not reach Hunter: {type(exc).__name__}") from exc
    finally:
        if own:
            http.close()
    if r.status_code == 401:
        raise HunterUnavailable("Hunter refused the API key.")
    if r.status_code in (402, 403, 429):
        raise HunterUnavailable("Hunter says this account is out of searches, or is being "
                                "rate limited. Searches reset monthly.")
    if r.status_code >= 400:
        try:
            detail = (r.json().get("errors") or [{}])[0].get("details") or r.text[:120]
        except ValueError:
            detail = r.text[:120]
        raise HunterUnavailable(f"Hunter answered {r.status_code}: {detail}")
    return r.json()


def domain_search(domain: str, *, client: httpx.Client | None = None) -> list[Found]:
    """Every address Hunter knows for the domain, best first. One search credit."""
    data = (_get("/domain-search", {"domain": domain, "limit": 10}, client=client).get("data") or {})
    out = []
    for row in data.get("emails") or []:
        email = normalize(row.get("value"))
        if not email:
            continue
        name = " ".join(x for x in (row.get("first_name"), row.get("last_name")) if x)
        out.append(Found(
            email=email, personal=(row.get("type") == "personal"), name=name,
            position=str(row.get("position") or ""), confidence=int(row.get("confidence") or 0),
            hunter_status=str((row.get("verification") or {}).get("status") or ""),
            sources=len(row.get("sources") or []),
        ))
    out.sort(key=lambda f: (not f.personal, -f.confidence))
    return out


def searches_left(*, client: httpx.Client | None = None) -> int | None:
    """What the account says is left this month, or None if it will not say."""
    try:
        data = _get("/account", {}, client=client).get("data") or {}
    except HunterUnavailable:
        return None
    searches = ((data.get("requests") or {}).get("searches") or {})
    if "available" in searches and "used" in searches:
        return max(0, int(searches["available"]) - int(searches["used"]))
    return None


def to_contact(found: Found, *, domain: str) -> dict[str, Any]:
    """A Hunter result in the shape of a discovered contact, verified."""
    verdict = verify(found.email)
    status, reason = verdict.status, verdict.reason
    if found.hunter_status == "invalid":
        status, reason = INVALID, "Hunter found the mailbox does not exist."
    elif found.hunter_status == "accept_all" and status == VALID:
        status, reason = RISKY, "The domain accepts every address, so this one could not be confirmed."
    seen = f" Seen in {found.sources} public source{'s' if found.sources != 1 else ''}." if found.sources else ""
    return {
        "email": found.email, "source": SOURCE,
        "kind": KIND_PERSONAL if found.personal else KIND_ROLE,
        "own_domain": found.email.split("@", 1)[1] == domain.lower(),
        "page_path": "", "name": found.name, "position": found.position,
        "confidence": found.confidence, "status": status, "reason": reason + seen,
    }
