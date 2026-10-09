"""A technical crawl of a prospect's site with the DataForSEO On-Page API.

Runs only when a person asks for it (Run technical audit on the prospect
page): a crawl costs credits per page and takes minutes, and most prospects
are never worked. The crawl is a task: post it, then read its summary until
DataForSEO says it finished. Shown in the console only, never on the public
report, so the labels below may name things a roofer would not.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from app.config import get_config

BASE = "https://api.dataforseo.com/v3/on_page"
TIMEOUT = 30.0


class OnPageUnavailable(RuntimeError):
    """No credentials, a refused task, or an answer we cannot read."""


# Page-level checks worth an operator's time, in the order they matter to a
# roofer's site, with the plain label the console shows. Counts are pages.
PAGE_CHECKS: tuple[tuple[str, str], ...] = (
    ("is_4xx_code", "Pages that return not found"),
    ("is_5xx_code", "Pages with server errors"),
    ("no_title", "Pages with no title"),
    ("no_description", "Pages with no meta description"),
    ("no_h1_tag", "Pages with no main heading (H1)"),
    ("title_too_long", "Titles too long to show in Google"),
    ("low_content_rate", "Thin pages, little text"),
    ("no_image_alt", "Pages with images missing alt text"),
    ("is_http", "Pages not on https"),
    ("https_to_http_links", "Secure pages linking to insecure ones"),
    ("redirect_chain", "Redirect chains"),
    ("high_loading_time", "Slow loading pages"),
    ("has_render_blocking_resources", "Pages held up by render-blocking files"),
    ("no_favicon", "Pages with no favicon"),
)
# Site-level counts that sit on page_metrics rather than in its checks.
METRIC_CHECKS: tuple[tuple[str, str], ...] = (
    ("broken_links", "Broken links"),
    ("broken_resources", "Broken images or files"),
    ("duplicate_title", "Pages sharing a title"),
    ("duplicate_description", "Pages sharing a meta description"),
    ("duplicate_content", "Near-duplicate pages"),
)


def _auth() -> tuple[str, str]:
    cfg = get_config()
    if not cfg.dataforseo_login or not cfg.dataforseo_password:
        raise OnPageUnavailable("DataForSEO credentials are not set (DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD).")
    return cfg.dataforseo_login, cfg.dataforseo_password


def _task(body: Mapping[str, Any]) -> Mapping[str, Any]:
    task = (body.get("tasks") or [{}])[0]
    if int(task.get("status_code") or 0) >= 40000:
        raise OnPageUnavailable(f"DataForSEO: {task.get('status_message') or 'task refused'}")
    return task


def start(domain: str, *, max_pages: int, client: httpx.Client | None = None) -> str:
    """Post a crawl of the domain. Returns the task id."""
    payload = [{"target": domain, "max_crawl_pages": int(max_pages), "load_resources": True,
                "enable_javascript": False, "store_raw_html": False}]
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        r = http.post(f"{BASE}/task_post", json=payload, auth=_auth())
    except httpx.HTTPError as exc:
        raise OnPageUnavailable(f"Could not reach DataForSEO: {type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
    if r.status_code >= 400:
        raise OnPageUnavailable(f"DataForSEO answered {r.status_code}")
    task_id = _task(r.json()).get("id")
    if not task_id:
        raise OnPageUnavailable("DataForSEO did not return a task id")
    return str(task_id)


def summary(task_id: str, *, client: httpx.Client | None = None) -> Mapping[str, Any] | None:
    """The crawl's summary once finished, or None while it is still crawling."""
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        r = http.get(f"{BASE}/summary/{task_id}", auth=_auth())
    except httpx.HTTPError as exc:
        raise OnPageUnavailable(f"Could not reach DataForSEO: {type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
    if r.status_code >= 400:
        raise OnPageUnavailable(f"DataForSEO answered {r.status_code}")
    result = (_task(r.json()).get("result") or [None])[0]
    if not result or result.get("crawl_progress") != "finished":
        return None
    return result


def distil(result: Mapping[str, Any]) -> dict[str, Any]:
    """The few numbers and the plain issue list the console shows."""
    domain = result.get("domain_info") or {}
    metrics = result.get("page_metrics") or {}
    checks = metrics.get("checks") or {}
    status = result.get("crawl_status") or {}
    issues = [{"key": k, "label": label, "count": int(metrics.get(k) or 0)}
              for k, label in METRIC_CHECKS if int(metrics.get(k) or 0) > 0]
    issues += [{"key": k, "label": label, "count": int(checks.get(k) or 0)}
               for k, label in PAGE_CHECKS if int(checks.get(k) or 0) > 0]
    site_checks = domain.get("checks") or {}
    ssl = (domain.get("ssl_info") or {}).get("valid_certificate")
    return {
        "health": round(float(metrics["onpage_score"])) if isinstance(metrics.get("onpage_score"), (int, float)) else None,
        "pages_crawled": int(status.get("pages_crawled") or domain.get("total_pages") or 0),
        "cms": str(domain.get("cms") or ""),
        "site": [
            {"label": "Valid security certificate", "ok": ssl},
            {"label": "Has a sitemap", "ok": site_checks.get("sitemap")},
            {"label": "Has a robots.txt", "ok": site_checks.get("robots_txt")},
        ],
        "issues": issues,
    }
