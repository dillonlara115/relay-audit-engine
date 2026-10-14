"""Google Places (New) ingest.

Text Search caps a single query at 60 results across three pages, so a metro
sweep runs several query variants and dedupes on place_id. That is how we get
to a hundred candidates without pretending one query returns them.

Every call goes through the Firestore read-through cache, 30 day TTL, so
re-running a sweep during development costs nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import httpx

from app.config import get_config
from app.store import firestore as store
from app.tools.crawl import registrable_host, normalize_url

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

SEARCH_FIELD_MASK = ",".join(
    (
        "nextPageToken",
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.primaryType",
        "places.primaryTypeDisplayName",
        "places.types",
        "places.businessStatus",
        "places.regularOpeningHours",
        "places.googleMapsUri",
        "places.reviews",
    )
)

# Text Search returns at most 60 per query. Variants widen the net and overlap
# heavily, which is fine: dedupe is on place_id.
DEFAULT_QUERIES: tuple[str, ...] = (
    "roofing contractor in {market}",
    "roof replacement {market}",
    "roofing company {market}",
    "roof repair {market}",
    "storm damage roof repair {market}",
)

# Smaller towns' operators often do not surface for a metro-name query at all:
# Google anchors text search on the named locality, and a Monument roofer can
# sit past result sixty for "roofing contractor in Colorado Springs" while
# being the first result for his own town. One query per enumerated locality
# closes that gap, and the fit gate already screens whatever else the wider
# net drags in.
LOCALITY_QUERY = "roofing contractor in {city}, {state}"


def queries_for_market(spec: Any) -> list[str]:
    """The metro-wide variants plus one query per enumerated locality.

    Returns fully formatted query strings. An unmapped market has no
    enumerated localities and gets the metro variants alone.
    """
    market = spec.query_market()
    out = [template.format(market=market) for template in DEFAULT_QUERIES]
    cities = sorted(getattr(spec, "cities", ()) or ())
    state = getattr(spec, "state", None)
    metro_name = spec.name.strip().lower()
    for city in cities:
        if city == metro_name:
            continue  # the metro variants already cover the anchor city
        out.append(LOCALITY_QUERY.format(city=city.title(), state=state or ""))
    return out

MAX_PAGES_PER_QUERY = 3


@dataclass
class PlaceRecord:
    """Flattened Places payload. Absent stays absent, never zero."""

    place_id: str
    business_name: str
    website_url: str | None = None
    gbp_phone: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lng: float | None = None
    rating: float | None = None
    review_count: int | None = None
    primary_type: str | None = None
    types: list[str] = field(default_factory=list)
    business_status: str | None = None
    maps_uri: str | None = None
    hours_published: bool | None = None
    weekday_hours: list[str] = field(default_factory=list)
    first_review_at: datetime | None = None
    latest_review_at: datetime | None = None
    review_sample_size: int | None = None

    @property
    def domain(self) -> str | None:
        return registrable_host(self.website_url) if self.website_url else None


def _component(components: Sequence[dict[str, Any]], wanted: str) -> str | None:
    for component in components or ():
        if wanted in (component.get("types") or []):
            return component.get("shortText") or component.get("longText")
    return None


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def flatten_place(payload: dict[str, Any]) -> PlaceRecord | None:
    place_id = payload.get("id")
    name = (payload.get("displayName") or {}).get("text")
    if not place_id or not name:
        return None

    components = payload.get("addressComponents") or []
    location = payload.get("location") or {}
    hours = payload.get("regularOpeningHours") or {}

    review_times = [
        parsed
        for parsed in (_parse_time(r.get("publishTime")) for r in payload.get("reviews") or [])
        if parsed
    ]

    return PlaceRecord(
        place_id=place_id,
        business_name=name,
        website_url=normalize_url(payload.get("websiteUri") or ""),
        gbp_phone=payload.get("nationalPhoneNumber") or payload.get("internationalPhoneNumber"),
        address=payload.get("formattedAddress"),
        city=_component(components, "locality") or _component(components, "postal_town"),
        state=_component(components, "administrative_area_level_1"),
        postal_code=_component(components, "postal_code"),
        lat=location.get("latitude"),
        lng=location.get("longitude"),
        rating=payload.get("rating"),
        review_count=payload.get("userRatingCount"),
        primary_type=payload.get("primaryType"),
        types=list(payload.get("types") or []),
        business_status=payload.get("businessStatus"),
        maps_uri=payload.get("googleMapsUri"),
        hours_published=bool(hours.get("weekdayDescriptions")) or None,
        weekday_hours=list(hours.get("weekdayDescriptions") or []),
        first_review_at=min(review_times) if review_times else None,
        latest_review_at=max(review_times) if review_times else None,
        review_sample_size=len(review_times) or None,
    )


async def _search_page(
    client: httpx.AsyncClient,
    api_key: str,
    text_query: str,
    page_token: str | None,
) -> dict[str, Any]:
    request = {"textQuery": text_query, "pageSize": 20}
    if page_token:
        request["pageToken"] = page_token

    cache_request = {"textQuery": text_query, "pageToken": page_token or ""}
    cached = await asyncio.to_thread(store.cache_get, "places", cache_request)
    if cached is not None:
        return cached

    resp = await client.post(
        SEARCH_URL,
        json=request,
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": SEARCH_FIELD_MASK,
            "Content-Type": "application/json",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    await asyncio.to_thread(store.cache_put, "places", cache_request, payload)
    return payload


async def search_market(
    market: str,
    *,
    queries: Iterable[str] | None = None,
    limit: int = 100,
) -> list[PlaceRecord]:
    """Run every query variant, dedupe on place_id, return up to `limit`."""
    cfg = get_config()
    cfg.require("places_api_key")

    templates = tuple(queries) if queries else DEFAULT_QUERIES
    found: dict[str, PlaceRecord] = {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        for template in templates:
            text_query = template.format(market=market) if "{market}" in template else template
            page_token: str | None = None
            for _page in range(MAX_PAGES_PER_QUERY):
                payload = await _search_page(client, cfg.places_api_key, text_query, page_token)
                for raw in payload.get("places") or []:
                    record = flatten_place(raw)
                    if record and record.place_id not in found:
                        found[record.place_id] = record
                page_token = payload.get("nextPageToken")
                if not page_token or len(found) >= limit:
                    break
            if len(found) >= limit:
                break

    return list(found.values())[:limit]


@dataclass
class IngestResult:
    market_id: str
    batch_id: str
    found: int
    upserted: int
    suppressed: int
    records: list[PlaceRecord]


async def ingest_market(
    market: str,
    *,
    batch_id: str,
    market_id: str,
    limit: int = 100,
    queries: Iterable[str] | None = None,
) -> IngestResult:
    """Search, suppression-screen, and upsert. Suppressed prospects are marked, not dropped."""
    records = await search_market(market, queries=queries, limit=limit)
    suppressions = await asyncio.to_thread(store.load_suppressions)

    survivors: list[PlaceRecord] = []
    suppressed_count = 0

    for record in records:
        hit = store.suppression_hit(
            suppressions,
            place_id=record.place_id,
            domain=record.domain,
            phone=record.gbp_phone,
        )
        fields = prospect_fields(record, market_id=market_id, batch_id=batch_id)
        if hit:
            fields["suppressed"] = True
            fields["suppressed_reason"] = hit
            suppressed_count += 1
        else:
            survivors.append(record)

        await asyncio.to_thread(store.upsert_prospect, record.place_id, fields)

    await asyncio.to_thread(
        store.bump_batch_counts, batch_id, ingested=len(records)
    )

    return IngestResult(
        market_id=market_id,
        batch_id=batch_id,
        found=len(records),
        upserted=len(records),
        suppressed=suppressed_count,
        records=survivors,
    )


def prospect_fields(record: PlaceRecord, *, market_id: str, batch_id: str) -> dict[str, Any]:
    """What a prospect document holds from Places, for a sweep or a single add."""
    return {
        "business_name": record.business_name,
        "website_url": record.website_url,
        "gbp_phone": record.gbp_phone,
        "address": record.address,
        "city": record.city,
        "state": record.state,
        "postal_code": record.postal_code,
        "lat": record.lat,
        "lng": record.lng,
        "rating": record.rating,
        "review_count": record.review_count,
        "primary_type": record.primary_type,
        "types": record.types,
        "business_status": record.business_status,
        "maps_uri": record.maps_uri,
        "hours_published": record.hours_published,
        "first_review_at": record.first_review_at,
        "latest_review_at": record.latest_review_at,
        "review_sample_size": record.review_sample_size,
        "domain": record.domain,
        "market_id": market_id,
        "latest_batch_id": batch_id,
    }


# ── one business, by hand ─────────────────────────────────────────────────────

FIND_LIMIT = 5


async def find_business(query: str, *, limit: int = FIND_LIMIT) -> list[PlaceRecord]:
    """One Text Search for a business a person names, say "Great Dane Roofing
    Colorado Springs". The first page only, through the same 30 day cache, so
    looking it up again to add it costs nothing."""
    cfg = get_config()
    cfg.require("places_api_key")
    query = " ".join(query.split())[:200]
    if not query:
        return []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        payload = await _search_page(client, cfg.places_api_key, query, None)
    records = [r for r in (flatten_place(raw) for raw in payload.get("places") or []) if r]
    return records[:limit]


NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"


async def town_at(lat: float, lng: float) -> tuple[str | None, str | None]:
    """(town, state) nearest a point, for a service-area listing that shows no
    address: the audit's local searches and the report both need a town.
    Cached like every Places call; a miss is (None, None), never a guess."""
    cfg = get_config()
    if not cfg.places_api_key:
        return None, None
    request = {"includedTypes": ["locality"], "maxResultCount": 1, "rankPreference": "DISTANCE",
               "locationRestriction": {"circle": {"center": {"latitude": round(lat, 4), "longitude": round(lng, 4)},
                                                  "radius": 25000.0}}}
    payload = await asyncio.to_thread(store.cache_get, "places_town", request)
    if payload is None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                r = await client.post(NEARBY_URL, json=request, headers={
                    "X-Goog-Api-Key": cfg.places_api_key,
                    "X-Goog-FieldMask": "places.displayName,places.addressComponents"})
            r.raise_for_status()
            payload = r.json()
        except (httpx.HTTPError, ValueError):
            return None, None
        await asyncio.to_thread(store.cache_put, "places_town", request, payload)
    place = (payload.get("places") or [None])[0]
    if not place:
        return None, None
    comps = place.get("addressComponents") or []
    return (_component(comps, "locality") or (place.get("displayName") or {}).get("text"),
            _component(comps, "administrative_area_level_1"))


async def town_center(city: str, state: str | None = None) -> tuple[float, float] | None:
    """Where Google puts the middle of a town, for a Local reach grid centred
    on the town a service-area business works rather than on its hidden base.
    Cached; None when there is no answer, never a guess."""
    cfg = get_config()
    if not cfg.places_api_key or not city:
        return None
    request = {"textQuery": f"{city}, {state}" if state else city, "includedType": "locality", "pageSize": 1}
    payload = await asyncio.to_thread(store.cache_get, "places_town_center", request)
    if payload is None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                r = await client.post(SEARCH_URL, json=request, headers={
                    "X-Goog-Api-Key": cfg.places_api_key, "X-Goog-FieldMask": "places.location,places.displayName"})
            r.raise_for_status()
            payload = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        await asyncio.to_thread(store.cache_put, "places_town_center", request, payload)
    loc = ((payload.get("places") or [{}])[0] or {}).get("location") or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        return None
    return float(loc["latitude"]), float(loc["longitude"])
