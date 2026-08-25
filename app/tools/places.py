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
            text_query = template.format(market=market)
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
        fields: dict[str, Any] = {
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
