"""A Google map under the Local reach grid.

One Static Maps image per run, fetched here on the server so the key never
reaches a browser, and kept with the audit's evidence. The pins are not drawn
into the image: the page lays them over it, placed with the same Web Mercator
projection Google draws with, so each sits on the exact spot it searched from
and can be clicked.
"""

from __future__ import annotations

import math
from typing import Iterable

import httpx

from app.config import get_config

ENDPOINT = "https://maps.googleapis.com/maps/api/staticmap"
SIZE = 640        # logical pixels a side, the largest Static Maps serves
SCALE = 2         # 1280 real pixels, sharp on a high density screen
FILL = 0.9        # the grid's share of the frame; the rest keeps edge pins whole
ZOOMS = range(16, 7, -1)
TIMEOUT = 30.0
# Quiet the map so the pins read first: no shops or bus stops, softer labels.
STYLES = ("feature:poi|visibility:off", "feature:transit|visibility:off",
          "feature:road|element:labels.icon|visibility:off")


class MapUnavailable(RuntimeError):
    pass


def _world(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """A point's pixel position on the whole world map at this zoom."""
    size = 256 * 2 ** zoom
    s = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    return (lng + 180) / 360 * size, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size


def offset(center: tuple[float, float], lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """Where a point sits in the frame, as percent from the left and top."""
    cx, cy = _world(*center, zoom)
    x, y = _world(lat, lng, zoom)
    return round(50 + (x - cx) / SIZE * 100, 2), round(50 + (y - cy) / SIZE * 100, 2)


def fit_zoom(center: tuple[float, float], points: Iterable[tuple[float, float]]) -> int:
    """The closest zoom that still shows every point inside the frame."""
    pts = list(points)
    for zoom in ZOOMS:
        if all(abs(x - 50) <= FILL * 50 and abs(y - 50) <= FILL * 50
               for x, y in (offset(center, lat, lng, zoom) for lat, lng in pts)):
            return zoom
    return ZOOMS[-1]


def fetch(center: tuple[float, float], zoom: int, *, client: httpx.Client | None = None) -> bytes:
    """The map image, PNG. Raises MapUnavailable rather than returning a
    Google error tile, which is also a 200 with an image in it."""
    key = get_config().places_api_key  # the server key; Static Maps is enabled on it
    if not key:
        raise MapUnavailable("No Google key set (GOOGLE_PLACES_API_KEY).")
    params = [("center", f"{center[0]},{center[1]}"), ("zoom", str(zoom)),
              ("size", f"{SIZE}x{SIZE}"), ("scale", str(SCALE)), ("maptype", "roadmap"),
              ("format", "png"), ("key", key)] + [("style", s) for s in STYLES]
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        r = http.get(ENDPOINT, params=params)
    except httpx.HTTPError as exc:
        raise MapUnavailable(f"Static Maps did not answer: {type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise MapUnavailable(f"Static Maps returned {r.status_code}: {r.text[:160] if r.status_code != 200 else 'not an image'}")
    return r.content
