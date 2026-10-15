"""How many people search for a roofer in a prospect's town, from Google's
own monthly figures (DataForSEO's Google Ads search volume).

One lookup per town, about nine cents, cached for 30 days. The town rides in
each phrase, the way DataForSEO's US-wide location expects. Google reports
nothing for phrases too rare to count and folds close variants into one
figure, so the phrasings are never added up: the report leads with the
biggest real figure, "at least", and lists the rest.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_config
from app.store import firestore as store

ENDPOINT = "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live"
US = 2840
TIMEOUT = 120.0
ATTEMPTS = 3
SHOWN = 5


class DemandUnavailable(RuntimeError):
    pass


def phrases(city: str, state: str | None = None) -> list[str]:
    """The ways a homeowner types it. Town first and town last both happen:
    in Prescott, "prescott roofing" drew 390 a month, "roofer prescott az" too
    few to count."""
    c = " ".join(city.lower().split())
    st = (state or "").lower().strip()
    out = [f"{c} roofing", f"{c} roofers", f"{c} roofer", f"{c} roof repair", f"{c} roofing company",
           f"roofing {c}", f"roofers {c}", f"roofer {c}", f"roof repair {c}", f"roof replacement {c}",
           f"roofing company {c}", f"roofing contractors {c}", f"roofers in {c}", f"roofing companies {c}"]
    if st:
        out += [f"roofer {c} {st}", f"roof repair {c} {st}", f"roofing {c} {st}", f"roofers {c} {st}",
                f"roof replacement {c} {st}", f"{c} {st} roofing"]
    return list(dict.fromkeys(out))


def look_up(city: str, state: str | None = None, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """{"rows": [{"phrase", "monthly"}...] biggest first, "top": the biggest}.
    Rows hold only phrases Google gave a figure for."""
    if not city:
        raise DemandUnavailable("No town on record to search for.")
    request = {"keywords": phrases(city, state), "location_code": US, "language_code": "en"}
    cached = store.cache_get("search_demand", request)
    if cached is None:
        cfg = get_config()
        if not cfg.dataforseo_login or not cfg.dataforseo_password:
            raise DemandUnavailable("DataForSEO credentials are not set.")
        http = client or httpx.Client(timeout=TIMEOUT)
        try:
            body = None
            for attempt in range(ATTEMPTS):
                try:
                    body = http.post(ENDPOINT, json=[request], auth=(cfg.dataforseo_login, cfg.dataforseo_password)).json()
                    break
                except (httpx.HTTPError, ValueError) as exc:
                    if attempt == ATTEMPTS - 1:
                        raise DemandUnavailable(f"Search figures did not come back ({type(exc).__name__}).") from exc
        finally:
            if client is None:
                http.close()
        task = ((body or {}).get("tasks") or [{}])[0]
        if int(task.get("status_code") or 0) >= 40000:
            raise DemandUnavailable(f"DataForSEO: {task.get('status_message') or 'refused'}")
        cached = {"rows": [{"phrase": str(it.get("keyword") or ""), "monthly": int(it["search_volume"])}
                           for it in task.get("result") or [] if isinstance(it.get("search_volume"), int)
                           and it["search_volume"] > 0]}
        store.cache_put("search_demand", request, cached)
    rows = sorted(cached.get("rows") or [], key=lambda r: -r["monthly"])
    return {"rows": rows[:SHOWN], "top": rows[0]["monthly"] if rows else 0}
