"""DataForSEO SERP client. Answers F8, F9 and F12.

The criteria doc defines exactly two searches, both scoped to the prospect's
metro:

    F8  map pack presence   top 3 for "roofer [city]"
    F9  organic presence    top 10 for "roof replacement [city]"
    F12 paid search         running Google Ads

One live request per query returns the whole page, so the map pack, the organic
list and the ads all come out of the same two calls. Results are cached for
seven days per the engine spec, because a metro's SERP does not move fast enough
to justify paying for it on every re-audit.

On matching: a SERP result belongs to this prospect when its domain matches the
prospect's registrable domain. Name matching is deliberately not used as a
fallback. "Triton Roofing" and "Triton Roofing and Restoration" are different
companies, and a wrong match here writes a rank into a report that is not his.

On F12: two searches cannot prove a contractor runs no ads anywhere, only that
no ad of his appeared for these two terms. The note says exactly that. Claiming
more would be inventing a finding, and an owner who is running ads on terms we
did not search would stop reading right there.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from app.config import get_config
from app.store import firestore as store
from app.tools.crawl import registrable_host

ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"

# Criteria doc thresholds.
MAP_PACK_TOP = 3
ORGANIC_TOP = 10

# A live SERP call runs a real search on their side. Slower than it looks.
DEFAULT_TIMEOUT = 90.0

MAP_PACK_QUERY = "roofer {city}"
ORGANIC_QUERY = "roof replacement {city}"

# DataForSEO location names spell the state out: "Colorado Springs,Colorado,
# United States". A MarketSpec stores the two-letter code, and sending that
# gets the whole task rejected with "Invalid Field: 'location_name'", which
# costs all three checks. Measured against the live API on Rampart Roofing.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def location_name_for(city: str, state: str | None) -> str:
    """The provider's location string for a metro.

    An unknown state is dropped rather than guessed at. "Colorado Springs,
    United States" still resolves; "Colorado Springs,XX,United States" is a
    rejected task and three skipped checks.
    """
    full = US_STATES.get((state or "").strip().upper())
    return f"{city},{full},United States" if full else f"{city},United States"

# DataForSEO groups every block on the page under one items list, tagged by
# type. These are the three we read.
LOCAL_PACK_TYPES = {"local_pack", "map"}
ORGANIC_TYPES = {"organic"}
PAID_TYPES = {"paid"}


def _domain(value: str | None) -> str | None:
    """Registrable domain, or None. Shared with the crawler so a match here
    means the same thing a match there does."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "//" not in text:
        text = f"https://{text}"
    return registrable_host(text)


@dataclass(frozen=True)
class SerpLook:
    """One query's worth of page, reduced to what the three checks ask."""

    ok: bool
    query: str
    location_name: str
    map_pack_rank: int | None = None
    map_pack_size: int = 0
    organic_rank: int | None = None
    organic_size: int = 0
    paid: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class SerpFacts:
    """Both searches together. What AuditContext.serp holds."""

    ok: bool
    looks: tuple[SerpLook, ...] = ()
    map_pack_rank: int | None = None
    organic_rank: int | None = None
    paid: bool = False
    queries: tuple[str, ...] = ()
    error: str | None = None

    @property
    def in_map_pack(self) -> bool:
        return self.map_pack_rank is not None and self.map_pack_rank <= MAP_PACK_TOP

    @property
    def in_organic(self) -> bool:
        return self.organic_rank is not None and self.organic_rank <= ORGANIC_TOP


def _rank_of(items: Sequence[Mapping[str, Any]], types: set[str],
             domain: str) -> tuple[int | None, int]:
    """1-based rank of our domain within one block type, and the block's size.

    Ranks are counted over the block as it appears, not taken from the
    provider's absolute rank_absolute, which counts every element on the page
    including the ones we are not looking at.
    """
    rank: int | None = None
    seen = 0
    for item in items:
        if str(item.get("type") or "") not in types:
            continue
        seen += 1
        if rank is None and _domain(item.get("domain") or item.get("url")) == domain:
            rank = seen
    return rank, seen


def _has_paid(items: Sequence[Mapping[str, Any]], domain: str) -> bool:
    for item in items:
        if str(item.get("type") or "") not in PAID_TYPES:
            continue
        if _domain(item.get("domain") or item.get("url")) == domain:
            return True
    return False


def flatten(payload: Mapping[str, Any], *, query: str, location_name: str,
            domain: str) -> SerpLook:
    """Provider response to one SerpLook. Never raises on shape."""
    tasks = payload.get("tasks") or []
    if not tasks:
        return SerpLook(ok=False, query=query, location_name=location_name,
                        error="response carried no tasks")

    task = tasks[0] or {}
    status_code = task.get("status_code")
    if status_code and int(status_code) >= 40000:
        return SerpLook(ok=False, query=query, location_name=location_name,
                        error=f"task {status_code}: {task.get('status_message')}"[:300])

    results = task.get("result") or []
    if not results:
        return SerpLook(ok=False, query=query, location_name=location_name,
                        error="task carried no result")

    items = (results[0] or {}).get("items") or []
    map_rank, map_size = _rank_of(items, LOCAL_PACK_TYPES, domain)
    org_rank, org_size = _rank_of(items, ORGANIC_TYPES, domain)
    return SerpLook(
        ok=True, query=query, location_name=location_name,
        map_pack_rank=map_rank, map_pack_size=map_size,
        organic_rank=org_rank, organic_size=org_size,
        paid=_has_paid(items, domain),
    )


async def look_up(query: str, *, location_name: str, domain: str,
                  client: httpx.AsyncClient | None = None,
                  timeout: float = DEFAULT_TIMEOUT,
                  fresh: bool = False) -> SerpLook:
    """One query, read through the cache. Seven day TTL. Never raises."""
    cfg = get_config()
    cfg.require("dataforseo_login")
    cfg.require("dataforseo_password")

    # The cache key carries the domain because the stored record is already
    # reduced to this prospect's ranks, not the raw page.
    cache_request = {"query": query, "location": location_name, "domain": domain}
    if not fresh:
        cached = await asyncio.to_thread(store.cache_get, "serp", cache_request)
        if cached:
            return SerpLook(**cached)

    body = [{
        "keyword": query,
        "location_name": location_name,
        "language_code": "en",
        "device": "mobile",
        "depth": 20,
    }]

    owned = client is None
    http_client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
    try:
        response = await http_client.post(
            ENDPOINT, json=body,
            auth=(cfg.dataforseo_login, cfg.dataforseo_password),
        )
        if response.status_code != 200:
            return SerpLook(ok=False, query=query, location_name=location_name,
                            error=f"DataForSEO returned {response.status_code}: "
                                  f"{response.text[:120]}"[:300])
        look = flatten(response.json(), query=query,
                       location_name=location_name, domain=domain)
    except httpx.HTTPError as exc:
        return SerpLook(ok=False, query=query, location_name=location_name,
                        error=f"{type(exc).__name__}: {exc}"[:300])
    except ValueError as exc:
        return SerpLook(ok=False, query=query, location_name=location_name,
                        error=f"bad SERP response: {exc}"[:300])
    finally:
        if owned:
            await http_client.aclose()

    if look.ok:
        await asyncio.to_thread(store.cache_put, "serp", cache_request, look.to_dict())
    return look


async def look_up_prospect(*, website_url: str | None, city: str,
                           state: str | None = None,
                           client: httpx.AsyncClient | None = None,
                           fresh: bool = False) -> SerpFacts:
    """Both defined searches for one prospect. Never raises.

    A prospect with no website has no domain to match, so there is nothing to
    look up and the checks skip rather than scoring him out of the map pack he
    may well be sitting in.
    """
    domain = _domain(website_url)
    if not domain:
        return SerpFacts(ok=False, error="no website to match a result against")
    if not city:
        return SerpFacts(ok=False, error="no city to search in")

    location_name = location_name_for(city, state)
    queries = (MAP_PACK_QUERY.format(city=city), ORGANIC_QUERY.format(city=city))

    owned = client is None
    http_client = client or httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_TIMEOUT))
    try:
        looks = tuple(await asyncio.gather(*(
            look_up(q, location_name=location_name, domain=domain,
                    client=http_client, fresh=fresh)
            for q in queries
        )))
    finally:
        if owned:
            await http_client.aclose()

    usable = [look for look in looks if look.ok]
    if not usable:
        first = looks[0] if looks else None
        return SerpFacts(ok=False, looks=looks, queries=queries,
                         error=(first.error if first else "no SERP result"))

    # F8 is defined on "roofer [city]" and F9 on "roof replacement [city]", but
    # a rank found on either page is still a rank he holds, so the best of the
    # two is the honest answer to "can a homeowner find him".
    map_ranks = [look.map_pack_rank for look in usable if look.map_pack_rank is not None]
    org_ranks = [look.organic_rank for look in usable if look.organic_rank is not None]
    return SerpFacts(
        ok=True,
        looks=looks,
        queries=queries,
        map_pack_rank=min(map_ranks) if map_ranks else None,
        organic_rank=min(org_ranks) if org_ranks else None,
        paid=any(look.paid for look in usable),
    )
