"""PageSpeed Insights client.

Two things worth knowing before reading this.

First, PSI returns both lab and field data. Lighthouse runs the page once on a
throttled emulated phone; CrUX reports what real Chrome users actually
experienced over the last 28 days. They disagree often and the field number is
the truer answer, so C4 prefers it and records which one it used.

Second, a full Lighthouse payload runs well past Firestore's 1 MiB document
limit, so the cache holds the distilled result rather than the raw response.
Raw payloads belong in Cloud Storage at the evidence stage, not in a cache
document that will fail to write on a slow site.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import httpx

from app.config import get_config
from app.store import firestore as store

ENDPOINT = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"

# Criteria doc thresholds.
MIN_PERFORMANCE_SCORE = 60
MAX_LCP_MS = 2500.0

# PSI runs a real Lighthouse pass server side. Thirty seconds is normal and a
# slow prospect site can push past a minute.
DEFAULT_TIMEOUT = 120.0

FIELD = "field"
LAB = "lab"


@dataclass(frozen=True)
class PsiResult:
    ok: bool
    url: str
    strategy: str = "mobile"
    final_url: str | None = None
    performance_score: int | None = None      # 0 to 100
    lcp_ms: float | None = None               # the one C4 judges
    lcp_source: str | None = None             # field or lab
    field_lcp_ms: float | None = None
    lab_lcp_ms: float | None = None
    field_lcp_category: str | None = None     # FAST / AVERAGE / SLOW
    fcp_ms: float | None = None
    cls: float | None = None
    tbt_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _audit_ms(audits: Mapping[str, Any], key: str) -> float | None:
    value = (audits.get(key) or {}).get("numericValue")
    return float(value) if isinstance(value, (int, float)) else None


def flatten(payload: Mapping[str, Any], url: str, strategy: str) -> PsiResult:
    """Distil a Lighthouse payload down to the numbers two checks need."""
    lighthouse = payload.get("lighthouseResult") or {}
    audits = lighthouse.get("audits") or {}
    categories = lighthouse.get("categories") or {}

    raw_score = (categories.get("performance") or {}).get("score")
    score = round(raw_score * 100) if isinstance(raw_score, (int, float)) else None

    lab_lcp = _audit_ms(audits, "largest-contentful-paint")

    # CrUX, when Chrome has enough real traffic on this origin to report it.
    experience = payload.get("loadingExperience") or {}
    metrics = experience.get("metrics") or {}
    lcp_metric = metrics.get("LARGEST_CONTENTFUL_PAINT_MS") or {}
    field_lcp = lcp_metric.get("percentile")
    field_lcp = float(field_lcp) if isinstance(field_lcp, (int, float)) else None

    # Real users beat a simulated phone whenever we have them.
    if field_lcp is not None:
        chosen, source = field_lcp, FIELD
    else:
        chosen, source = lab_lcp, (LAB if lab_lcp is not None else None)

    return PsiResult(
        ok=score is not None or chosen is not None,
        url=url,
        strategy=strategy,
        final_url=lighthouse.get("finalUrl") or lighthouse.get("requestedUrl"),
        performance_score=score,
        lcp_ms=chosen,
        lcp_source=source,
        field_lcp_ms=field_lcp,
        lab_lcp_ms=lab_lcp,
        field_lcp_category=lcp_metric.get("category"),
        fcp_ms=_audit_ms(audits, "first-contentful-paint"),
        cls=(audits.get("cumulative-layout-shift") or {}).get("numericValue"),
        tbt_ms=_audit_ms(audits, "total-blocking-time"),
    )


async def analyze(
    url: str,
    *,
    strategy: str = "mobile",
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    fresh: bool = False,
) -> PsiResult:
    """Read through cache, three day TTL. Never raises."""
    cfg = get_config()
    cfg.require("pagespeed_api_key")

    cache_request = {"url": url, "strategy": strategy}
    if not fresh:
        cached = await asyncio.to_thread(store.cache_get, "psi", cache_request)
        if cached:
            return PsiResult(**cached)

    params = {
        "url": url,
        "strategy": strategy,
        "category": "PERFORMANCE",
        "key": cfg.pagespeed_api_key,
    }

    owned = client is None
    http_client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
    try:
        response = await http_client.get(ENDPOINT, params=params)
        if response.status_code != 200:
            detail = ""
            try:
                detail = ((response.json() or {}).get("error") or {}).get("message", "")
            except ValueError:
                detail = response.text[:120]
            return PsiResult(ok=False, url=url, strategy=strategy,
                             error=f"PSI returned {response.status_code}: {detail}"[:300])
        result = flatten(response.json(), url, strategy)
    except httpx.HTTPError as exc:
        return PsiResult(ok=False, url=url, strategy=strategy,
                         error=f"{type(exc).__name__}: {exc}"[:300])
    except ValueError as exc:
        return PsiResult(ok=False, url=url, strategy=strategy, error=f"bad PSI response: {exc}")
    finally:
        if owned:
            await http_client.aclose()

    if result.ok:
        # Only the distilled record is cached. The raw Lighthouse payload is
        # evidence and belongs in Cloud Storage, not in a Firestore document
        # that would blow the 1 MiB limit on a heavy site.
        await asyncio.to_thread(store.cache_put, "psi", cache_request, result.to_dict())
    return result
