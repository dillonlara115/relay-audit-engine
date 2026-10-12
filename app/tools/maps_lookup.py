"""Find one business on Google Maps itself, for when the Places API cannot.

The Places API is a data feed, and it lags the map: a listing that is new or
recently changed can show on Google Maps for weeks before Text Search or
autocomplete returns it (Sun Spirit Roofing LLC in Prescott, October 2026:
on the map with 26 reviews, absent from every Places query). DataForSEO's
Maps search reads the map the way a person does, so Add a roofer falls back to
it when Places comes up empty, and only when a person asks: each search is
paid. Results go through the same Firestore cache, so looking one up again to
add it is free.

What comes back is shaped into the same PlaceRecord a sweep stores. The map
gives no review dates and no opening hours, so those stay absent, and the
checks that need them skip rather than guess.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

from app.config import get_config
from app.store import firestore as store
from app.tools.crawl import normalize_url
from app.tools.places import PlaceRecord

ENDPOINT = "https://api.dataforseo.com/v3/serp/google/maps/live/advanced"
US = 2840            # DataForSEO's United States; the town rides in the query
LIMIT = 5
TIMEOUT = 90.0
ATTEMPTS = 2         # DataForSEO sometimes drops a connection; one retry is enough


class MapsLookupUnavailable(RuntimeError):
    pass


def to_record(item: Mapping[str, Any]) -> PlaceRecord | None:
    place_id = str(item.get("place_id") or "").strip()
    title = str(item.get("title") or "").strip()
    if not place_id or not title:
        return None
    info = item.get("address_info") or {}
    rating = item.get("rating") or {}
    cid = item.get("cid")
    category = str(item.get("category") or "")
    hours = (item.get("work_time") or {}).get("work_hours") or {}
    closed = hours.get("current_status") in ("permanently_closed", "closed_forever")
    return PlaceRecord(
        place_id=place_id,
        business_name=title,
        website_url=normalize_url(str(item.get("url") or "")) if item.get("url") else None,
        gbp_phone=item.get("phone") or None,
        address=item.get("address") or None,
        city=info.get("city") or None,
        state=info.get("region") or None,
        postal_code=info.get("zip") or None,
        lat=item.get("latitude"),
        lng=item.get("longitude"),
        rating=rating.get("value"),
        review_count=rating.get("votes_count"),
        primary_type="roofing_contractor" if "roof" in category.lower() else None,
        types=[c for c in [category, *(item.get("additional_categories") or [])] if c],
        business_status="CLOSED_PERMANENTLY" if closed else "OPERATIONAL",
        maps_uri=f"https://maps.google.com/?cid={cid}" if cid else None,
    )


async def find(query: str, *, client: httpx.AsyncClient | None = None) -> list[PlaceRecord]:
    """Up to LIMIT businesses Google Maps shows for the query."""
    query = " ".join(query.split())[:200]
    if not query:
        return []
    cfg = get_config()
    if not cfg.dataforseo_login or not cfg.dataforseo_password:
        raise MapsLookupUnavailable("DataForSEO credentials are not set.")
    request = {"keyword": query, "location_code": US, "language_code": "en", "device": "desktop", "depth": 10}
    cached = await asyncio.to_thread(store.cache_get, "maps_lookup", request)
    if cached is None:
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT))
        try:
            body = None
            for attempt in range(ATTEMPTS):
                try:
                    r = await http.post(ENDPOINT, json=[request],
                                        auth=(cfg.dataforseo_login, cfg.dataforseo_password))
                    body = r.json()
                    break
                except (httpx.HTTPError, ValueError) as exc:
                    if attempt == ATTEMPTS - 1:
                        raise MapsLookupUnavailable(f"Google Maps search did not answer ({type(exc).__name__}).") from exc
        finally:
            if owned:
                await http.aclose()
        task = ((body or {}).get("tasks") or [{}])[0]
        if int(task.get("status_code") or 0) >= 40000:
            raise MapsLookupUnavailable(f"DataForSEO: {task.get('status_message') or 'refused'}")
        items = (((task.get("result") or [{}])[0] or {}).get("items") or [])
        cached = {"items": [i for i in items if i.get("type") in ("maps_search", None)][:LIMIT * 2]}
        await asyncio.to_thread(store.cache_put, "maps_lookup", request, cached)
    records = [r for r in (to_record(i) for i in cached.get("items") or []) if r][:LIMIT]
    # A service-area listing hides its address, town included. The map still
    # places it, so the town comes from the nearest one to that point.
    from dataclasses import replace

    from app.tools.places import town_at

    out = []
    for r in records:
        if not r.city and r.lat is not None and r.lng is not None:
            city, state = await town_at(r.lat, r.lng)
            r = replace(r, city=city, state=r.state or state)
        out.append(r)
    return out
