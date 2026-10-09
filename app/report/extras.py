"""Optional report sections: Google's speed test and a check of every page.

Off unless a person turns one on for a prospect. Turning one on takes a
snapshot of today's results into the audit (report_extras), so the report
stays a record of one day: a later Technical run changes the console, never a
page the owner already has, until someone turns the section off and on again.

Owner language. The speed test is Google's, named as Google's, because anyone
can run it themselves; nothing here uses our own vocabulary, and the payload
test walks these keys too.
"""

from __future__ import annotations

from typing import Any, Mapping

RINGS = (("performance", "Speed"), ("accessibility", "Accessibility"),
         ("best_practices", "Best practices"), ("seo", "Search basics"))

# What each crawl issue means to an owner, singular and plural.
OWNER_LINES: dict[str, tuple[str, str]] = {
    "is_4xx_code": ("page shows a \"page not found\" error", "pages show a \"page not found\" error"),
    "is_5xx_code": ("page fails with a server error", "pages fail with a server error"),
    "broken_links": ("link leads nowhere", "links lead nowhere"),
    "broken_resources": ("image or file doesn't load", "images or files don't load"),
    "no_title": ("page has no title for Google to show", "pages have no title for Google to show"),
    "duplicate_title": ("page shares its title with another", "pages share a title with another page"),
    "duplicate_content": ("page is a near copy of another", "pages are near copies of each other"),
    "no_h1_tag": ("page has no main heading", "pages have no main heading"),
    "is_http": ("page isn't on a secure connection", "pages aren't on a secure connection"),
    "no_description": ("page has no summary for Google to show", "pages have no summary for Google to show"),
    "title_too_long": ("title is too long for Google to show in full", "titles are too long for Google to show in full"),
    "low_content_rate": ("page has very little text on it", "pages have very little text on them"),
    "https_to_http_links": ("secure page links to an insecure one", "secure pages link to insecure ones"),
}
SHOWN = tuple(OWNER_LINES)


def _secs(ms: Any) -> str | None:
    return f"{ms / 1000:.1f} seconds" if isinstance(ms, (int, float)) else None


def speed_snapshot(lighthouse: Mapping[str, Any]) -> dict[str, Any] | None:
    scores = lighthouse.get("scores") or {}
    if not any(isinstance(scores.get(k), (int, float)) for k, _ in RINGS):
        return None
    m = lighthouse.get("metrics") or {}
    measured = lighthouse.get("measured_at")
    return {
        "ratings": [{"label": label, "value": scores.get(key)} for key, label in RINGS
                    if isinstance(scores.get(key), (int, float))],
        "main_content": _secs(m.get("lcp_ms")),
        "real_visitors": m.get("lcp_source") == "field",
        "looked_complete": _secs(m.get("speed_index_ms")),
        "test_url": str(lighthouse.get("psi_url") or ""),
        "measured": measured.strftime("%B %-d, %Y") if hasattr(measured, "strftime") else "",
    }


def site_check_snapshot(technical: Mapping[str, Any], *, text_unreliable: bool = False) -> dict[str, Any] | None:
    pages = int(technical.get("pages_crawled") or 0)
    if technical.get("status") != "done" or not pages:
        return None
    lines = []
    for issue in technical.get("issues") or []:
        key, count = issue.get("key"), int(issue.get("count") or 0)
        if key not in SHOWN or not count or (key == "low_content_rate" and text_unreliable):
            continue
        one, many = OWNER_LINES[key]
        lines.append({"key": key, "count": count, "text": f"{count} {one if count == 1 else many}"})
    lines.sort(key=lambda line: SHOWN.index(line["key"]))
    health = technical.get("health")
    return {"pages": pages, "health": health if isinstance(health, (int, float)) else None,
            "lines": [line["text"] for line in lines]}


def snapshot(audit: Mapping[str, Any], *, speed: bool, site_check: bool) -> dict[str, Any]:
    """What report_extras should hold for the sections switched on."""
    from app.tools import stack

    technical = audit.get("technical") or {}
    unreliable = bool(technical.get("browser")) or stack.any_of(stack.found_on(audit), "delays_scripts")
    return {
        "speed": speed_snapshot(audit.get("lighthouse") or {}) if speed else None,
        "site_check": site_check_snapshot(technical, text_unreliable=unreliable) if site_check else None,
    }
