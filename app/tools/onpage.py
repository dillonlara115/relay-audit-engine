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
    # Text against page code, not a word count: a 1,151 word homepage was
    # flagged. Not thin content, so never called that.
    ("low_content_rate", "Pages heavy on code for their text"),
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
LABELS = dict(PAGE_CHECKS + METRIC_CHECKS)

DETAIL_LIMIT = 100        # rows a detail list shows
DETAIL_PAGES_FOR = 25     # broken files whose pages are looked up, one call each


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


def start(domain: str, *, max_pages: int, browser: bool = False,
          client: httpx.Client | None = None) -> str:
    """Post a crawl of the domain. Returns the task id.

    browser=True loads every page in a real browser. Some hosts put a robot
    check in front of the site ("Robot Challenge Screen") that a plain fetch
    never gets past: the crawl finishes in seconds with no pages. A browser
    gets through, at roughly thirty times the cost per page."""
    payload = [{"target": domain, "max_crawl_pages": int(max_pages), "load_resources": True,
                "enable_javascript": browser, "store_raw_html": False}]
    if browser:
        payload[0]["enable_browser_rendering"] = True
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


def pages_crawled(result: Mapping[str, Any]) -> int:
    status = result.get("crawl_status") or {}
    return int(status.get("pages_crawled") or (result.get("domain_info") or {}).get("total_pages") or 0)


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


# ── One issue, item by item ──────────────────────────────────────────────────
# Reading a finished crawl back costs nothing; DataForSEO keeps it 30 days
# and the console caches what it read for as long.


def _post(path: str, body: Mapping[str, Any], client: httpx.Client) -> Mapping[str, Any]:
    try:
        r = client.post(f"{BASE}/{path}", json=[body], auth=_auth())
    except httpx.HTTPError as exc:
        raise OnPageUnavailable(f"Could not reach DataForSEO: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        raise OnPageUnavailable(f"DataForSEO answered {r.status_code}")
    return (_task(r.json()).get("result") or [{}])[0] or {}


def _secs(ms: Any) -> str:
    return f"{ms / 1000:.1f} s" if isinstance(ms, (int, float)) else ""


def detail(task_id: str, key: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """The items behind one issue count: {columns, rows, total, groups?}.
    rows are lists of cells; a cell starting with http is shown as a link."""
    if key not in LABELS:
        raise OnPageUnavailable(f"no such issue: {key}")
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        if key == "broken_resources":
            res = _post("resources", {"id": task_id, "filters": [["checks.is_broken", "=", True]],
                                      "limit": DETAIL_LIMIT}, http)
            items = res.get("items") or []

            def found_on(it: Mapping[str, Any]) -> str:
                on = _post("pages_by_resource", {"id": task_id, "url": it.get("url"), "limit": 3}, http)
                pages = [p.get("url") for p in on.get("items") or [] if p.get("url")]
                if not pages:
                    return ""
                more = int(on.get("total_items_count") or len(pages)) - 1
                return pages[0] + (f" (+{more} more)" if more > 0 else "")

            # One lookup per file, so they run side by side: nine files took
            # fifteen seconds one after another.
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=8) as pool:
                wheres = list(pool.map(found_on, items[:DETAIL_PAGES_FOR]))
            wheres += [""] * (len(items) - len(wheres))
            rows = [[it.get("url") or "", str(it.get("status_code") or "no answer"), where]
                    for it, where in zip(items, wheres)]
            return {"columns": ["File", "Status", "Found on"], "rows": rows,
                    "total": int(res.get("total_items_count") or len(rows))}
        if key == "broken_links":
            res = _post("links", {"id": task_id, "filters": [["is_broken", "=", True]], "limit": DETAIL_LIMIT}, http)
            rows = [[it.get("link_to") or "", str(it.get("page_to_status_code") or "no answer"),
                     it.get("link_from") or "", str(it.get("text") or "")] for it in res.get("items") or []]
            return {"columns": ["Link goes to", "Status", "On page", "Link text"], "rows": rows,
                    "total": int(res.get("total_items_count") or len(rows))}
        if key in ("duplicate_title", "duplicate_description"):
            res = _post("duplicate_tags", {"id": task_id, "type": key, "limit": DETAIL_LIMIT}, http)
            groups = [{"shared": str(it.get("accumulator") or ""), "count": int(it.get("total_count") or 0),
                       "pages": [p.get("url") for p in it.get("pages") or [] if p.get("url")]}
                      for it in res.get("items") or []]
            return {"columns": [], "rows": [], "groups": groups,
                    "total": sum(g["count"] for g in groups)}
        # Page-level: the pages the check flagged, and the one number that says why.
        flag = ["duplicate_content", "=", True] if key == "duplicate_content" else [f"checks.{key}", "=", True]
        res = _post("pages", {"id": task_id, "filters": [flag], "limit": DETAIL_LIMIT}, http)
        extra_col, extra = {
            "title_too_long": ("Title length", lambda it: str((it.get("meta") or {}).get("title_length") or "")),
            "low_content_rate": ("Words on page", lambda it: str(((it.get("meta") or {}).get("content") or {}).get("plain_text_word_count") or "")),
            "no_image_alt": ("Images on page", lambda it: str((it.get("meta") or {}).get("images_count") or "")),
            "high_loading_time": ("Load time", lambda it: _secs((it.get("page_timing") or {}).get("duration_time"))),
            "redirect_chain": ("Status", lambda it: str(it.get("status_code") or "")),
            "is_4xx_code": ("Status", lambda it: str(it.get("status_code") or "")),
            "is_5xx_code": ("Status", lambda it: str(it.get("status_code") or "")),
        }.get(key, ("", None))
        rows = []
        for it in res.get("items") or []:
            row = [it.get("url") or "", str((it.get("meta") or {}).get("title") or "")]
            if extra:
                row.append(extra(it))
            rows.append(row)
        return {"columns": ["Page", "Title"] + ([extra_col] if extra else []), "rows": rows,
                "total": int(res.get("total_items_count") or len(rows))}
    finally:
        if client is None:
            http.close()
