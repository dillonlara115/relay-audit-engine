"""Optional report sections: Google's speed test and a check of every page.

The speed test is off unless a person turns it on. The page check is on by
default: when a crawl finishes, and again at publish, it is added unless a
person has saved a choice for this prospect, and never onto a report that is
already out. Either way it is a snapshot of one day's results into the audit
(report_extras), so a later Technical run changes the console, never a page
the owner already has, until someone saves the section again.

The page check names the actual pages: up to ITEMS_SHOWN under each problem,
read from the crawl already on record (free), each linked to the page on the
owner's own site where the problem sits.

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


ITEMS_SHOWN = 5


def path_of(url: str) -> str:
    """A URL as the owner would recognise it on their own site: the path."""
    path = _full_path(url)
    if path == "/":
        return "Homepage"
    return path if len(path) <= 70 else path[:67] + "..."


def _full_path(url: str) -> str:
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url or "")
    return unquote(parts.path or "/") if parts.scheme else unquote(url or "")


def _file_name(url: str) -> str:
    """The file's own name, from the whole path: shortening first cut
    'Untitled-27-1024x574.png' to 'Untitled-27-10...'."""
    name = _full_path(url).rstrip("/").rsplit("/", 1)[-1]
    return (name if len(name) <= 60 else name[:57] + "...") or path_of(url)


def _clean(text: str) -> str | None:
    """Their own site's words, under our copy rules: dashes cleaned, and None
    when what is left would trip the report's vocabulary check."""
    from app.copy_rules import sanitize
    from app.report.data import forbidden_terms_in

    cleaned, _ = sanitize(" ".join(str(text or "").split()))
    return None if forbidden_terms_in(cleaned) else cleaned


def items_for(key: str, found: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """(up to ITEMS_SHOWN items, how many there are in all) for one problem.
    Each item: link (a path to show), href (their page, or None), note."""
    from app.tools import onpage

    out: list[dict[str, Any]] = []
    rows = list(found.get("rows") or [])
    total = int(found.get("total") or len(rows))
    if key in onpage.BROKEN_KEYS:
        rows, total = onpage.real_breaks(found)
    if key == "broken_links":
        for to, _status, frm, text in ((r + ["", "", "", ""])[:4] for r in rows):
            note = f"links to {path_of(to)}" + (f" (\u201c{text}\u201d)" if text else "")
            out.append({"link": path_of(frm), "href": frm or None, "note": note})
    elif key == "broken_resources":
        for file, _status, where in ((r + ["", "", ""])[:3] for r in rows):
            page, _, more = where.partition(" (+")
            others = int(more.split(" ", 1)[0]) if more[:1].isdigit() else 0
            on = f", on {others + 1} pages" if others else ""
            out.append({"link": path_of(page) if page else _file_name(file), "href": page or None,
                        "note": f"{_file_name(file)} doesn't load{on}" if page else "doesn't load"})
    elif key == "duplicate_title":
        for g in found.get("groups") or []:
            for page in g.get("pages") or []:
                out.append({"link": path_of(page), "href": page,
                            "note": f"shares the title \u201c{str(g.get('shared') or '')[:60]}\u201d"})
    else:
        for row in rows:
            page, extra = row[0], (row[2] if len(row) > 2 else "")
            note = {"title_too_long": f"title is {extra} characters" if extra else "",
                    "is_4xx_code": "page not found", "is_5xx_code": "server error"}.get(key, "")
            out.append({"link": path_of(page), "href": page or None, "note": note})
    kept = []
    for item in out:
        note = _clean(item["note"]) if item["note"] else ""
        link = _clean(item["link"])
        if note is None or not link:
            continue
        kept.append({**item, "note": note, "link": link})
        if len(kept) == ITEMS_SHOWN:
            break
    if key == "duplicate_title":
        total = len(out)
    return kept, total


def site_check_snapshot(technical: Mapping[str, Any], *, with_items: bool = True) -> dict[str, Any] | None:
    pages = int(technical.get("pages_crawled") or 0)
    if technical.get("status") != "done" or not pages:
        return None
    from app.tools import onpage

    task_id = technical.get("task_id")
    lines = []
    for issue in technical.get("issues") or []:
        key, count = issue.get("key"), int(issue.get("count") or 0)
        if key not in SHOWN or not count:
            continue
        items: list[dict[str, Any]] = []
        total = 0
        if with_items and task_id:
            try:
                found = onpage.cached_detail(str(task_id), key)
            except onpage.OnPageUnavailable:
                found = None
            if found is not None:
                items, total = items_for(key, found)
                if key in onpage.BROKEN_KEYS:
                    # Count what is really broken, once each: the crawl's own
                    # figure counts a file once per page and counts refusals.
                    count = total
                    if not count:
                        continue
        one, many = OWNER_LINES[key]
        lines.append({"key": key, "text": f"{count} {one if count == 1 else many}",
                      "items": items, "more": max(0, total - len(items)) if items else 0})
    lines.sort(key=lambda line: SHOWN.index(line["key"]))
    health = technical.get("health")
    return {"pages": pages, "health": health if isinstance(health, (int, float)) else None,
            "lines": lines}


def default_site_check(audit: Mapping[str, Any]) -> dict[str, Any] | None:
    """The report_extras write that puts the page check on by default, or None
    when a person has chosen, the report is already out, or there is nothing
    to show."""
    extras = audit.get("report_extras") or {}
    if extras.get("chosen") or extras.get("site_check") or audit.get("report_slug"):
        return None
    snap = site_check_snapshot(audit.get("technical") or {})
    if not snap:
        return None
    return {"report_extras": {"site_check": snap, "default": True}}


def snapshot(audit: Mapping[str, Any], *, speed: bool, site_check: bool) -> dict[str, Any]:
    """What report_extras should hold for the sections switched on."""
    return {
        "speed": speed_snapshot(audit.get("lighthouse") or {}) if speed else None,
        "site_check": site_check_snapshot(audit.get("technical") or {}) if site_check else None,
    }
