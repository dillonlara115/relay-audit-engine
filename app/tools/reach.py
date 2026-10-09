"""Local reach: where a roofer shows up in Google Maps across the area around
their business, and who shows up instead.

F8 asks one question from one spot, the middle of town. Maps results change
block by block, so this lays a square grid of points around the business and
runs the same Maps search from each one through DataForSEO's Google Maps
endpoint, which takes exact coordinates. Each point is one paid search, so
this runs only when a person asks, and the grid is capped.

The business is found in each result list by its Google place id, which is
our prospect id, so a rival with a similar name is never mistaken for it.
Console only.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from app.config import get_config

ENDPOINT = "https://api.dataforseo.com/v3/serp/google/maps/live/advanced"
TIMEOUT = 60.0
DEPTH = 20            # positions read per point; beyond this a point reads "20+"
ZOOM = "13z"          # roughly a few miles across, how a phone frames a local search
PARALLEL = 8

KEYWORDS = ("roofer", "roof repair", "roof replacement", "roofing contractor")
GRID_SIZES = (5, 7)
RADII_MILES = (3, 5, 10)


class ReachUnavailable(RuntimeError):
    """No credentials, or not one point came back."""


@dataclass(frozen=True)
class Point:
    row: int
    col: int
    lat: float
    lng: float


@dataclass
class Spot:
    row: int
    col: int
    lat: float
    lng: float
    rank: int | None = None          # their position, 1 based; None when not in the top DEPTH
    top: list[dict[str, Any]] = field(default_factory=list)   # the first three, as listed
    error: str = ""


def grid(lat: float, lng: float, *, size: int, radius_miles: float) -> list[Point]:
    """size x size points, the business at the centre, the edges radius_miles
    out north, south, east and west. Row 0 is the north edge."""
    if size < 2:
        return [Point(0, 0, lat, lng)]
    step = 2 * radius_miles / (size - 1)
    half = (size - 1) / 2
    lat_mile = 1 / 69.0
    lng_mile = 1 / (69.172 * max(math.cos(math.radians(lat)), 0.01))
    return [Point(r, c, round(lat + (half - r) * step * lat_mile, 6),
                  round(lng + (c - half) * step * lng_mile, 6))
            for r in range(size) for c in range(size)]


def _match(item: Mapping[str, Any], place_id: str, domain: str, name: str) -> bool:
    if place_id and item.get("place_id") == place_id:
        return True
    if domain and str(item.get("domain") or "").lower().removeprefix("www.") == domain:
        return True
    return bool(name) and str(item.get("title") or "").strip().lower() == name


def read_point(body: Mapping[str, Any], *, place_id: str, domain: str, name: str) -> tuple[int | None, list[dict[str, Any]], float]:
    """(their rank, the first three, what the call cost) from one response."""
    task = (body.get("tasks") or [{}])[0]
    if int(task.get("status_code") or 0) >= 40000:
        raise ReachUnavailable(f"DataForSEO: {task.get('status_message') or 'refused'}")
    cost = float(task.get("cost") or body.get("cost") or 0)
    result = (task.get("result") or [{}])[0] or {}
    items = [i for i in result.get("items") or [] if i.get("type") in ("maps_search", None)]
    rank = None
    top = []
    for item in items:
        position = int(item.get("rank_group") or item.get("rank_absolute") or 0)
        mine = _match(item, place_id, domain, name)
        if mine and rank is None and position:
            rank = position
        if position and position <= 3:
            rating = item.get("rating") or {}
            top.append({"title": str(item.get("title") or ""), "place_id": str(item.get("place_id") or ""),
                        "rating": rating.get("value"), "reviews": rating.get("votes_count"),
                        "mine": mine})
    return rank, top[:3], cost


async def run(*, place_id: str, domain: str, name: str, lat: float, lng: float, keyword: str,
              size: int, radius_miles: float, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Search every point, then sum it up. Raises only if nothing came back."""
    cfg = get_config()
    if not cfg.dataforseo_login or not cfg.dataforseo_password:
        raise ReachUnavailable("DataForSEO credentials are not set (DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD).")
    points = grid(lat, lng, size=size, radius_miles=radius_miles)
    domain = (domain or "").lower().removeprefix("www.")
    name = (name or "").strip().lower()
    gate = asyncio.Semaphore(PARALLEL)
    owned = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT))
    spent = [0.0]

    async def one(p: Point) -> Spot:
        spot = Spot(p.row, p.col, p.lat, p.lng)
        body = [{"keyword": keyword, "location_coordinate": f"{p.lat},{p.lng},{ZOOM}",
                 "language_code": "en", "device": "mobile", "os": "android", "depth": DEPTH}]
        async with gate:
            try:
                r = await http.post(ENDPOINT, json=body, auth=(cfg.dataforseo_login, cfg.dataforseo_password))
                if r.status_code != 200:
                    spot.error = f"DataForSEO returned {r.status_code}"
                    return spot
                spot.rank, spot.top, cost = read_point(r.json(), place_id=place_id, domain=domain, name=name)
                spent[0] += cost
            except (httpx.HTTPError, ValueError, ReachUnavailable) as exc:
                spot.error = str(exc)[:160] or type(exc).__name__
        return spot

    try:
        spots = await asyncio.gather(*(one(p) for p in points))
    finally:
        if owned:
            await http.aclose()
    answered = [s for s in spots if not s.error]
    if not answered:
        raise ReachUnavailable(spots[0].error if spots else "no points to search")
    return summarize(spots, keyword=keyword, size=size, radius_miles=radius_miles,
                     cost=round(spent[0], 4))


def summarize(spots: list[Spot], *, keyword: str, size: int, radius_miles: float,
              cost: float) -> dict[str, Any]:
    answered = [s for s in spots if not s.error]
    ranks = [s.rank for s in answered if s.rank]
    rivals: dict[str, dict[str, Any]] = {}
    for s in answered:
        for t in s.top:
            if t["mine"]:
                continue
            key = t["place_id"] or t["title"].lower()
            row = rivals.setdefault(key, {"title": t["title"], "rating": t["rating"],
                                          "reviews": t["reviews"], "spots": 0})
            row["spots"] += 1
    competitors = sorted(rivals.values(), key=lambda r: (-r["spots"], -(r["reviews"] or 0)))[:5]
    return {
        "keyword": keyword, "size": size, "radius_miles": radius_miles, "cost": cost,
        "points": [{"row": s.row, "col": s.col, "lat": s.lat, "lng": s.lng, "rank": s.rank,
                    "top": [t["title"] for t in s.top], "error": s.error} for s in spots],
        "answered": len(answered),
        "top3": sum(1 for r in ranks if r <= 3),
        "found": len(ranks),
        "average_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "competitors": competitors,
    }
