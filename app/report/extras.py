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


def positions_snapshot(checks: list[Mapping[str, Any]], *, city: str,
                       reach: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Where they rank, from searches the audit and Local reach already made.
    Raw numbers only: the report words them when it renders, so a wording fix
    reaches reports already saved. None when nothing was measured."""
    by = {str(c.get("code")): c for c in checks}
    rows: list[dict[str, Any]] = []
    f8 = by.get("F8") or {}
    if f8.get("status") in ("pass", "fail"):
        rows.append({"kind": "maps", "query": f"roofer {city}".strip(),
                     "rank": (f8.get("observed") or {}).get("map_pack_rank")})
    f9 = by.get("F9") or {}
    if f9.get("status") in ("pass", "fail"):
        rows.append({"kind": "search", "query": f"roof replacement {city}".strip(),
                     "rank": (f9.get("observed") or {}).get("organic_rank")})
    r = reach or {}
    if r.get("status") == "done" and r.get("answered"):
        rows.append({"kind": "grid", "query": str(r.get("keyword") or ""), "top3": int(r.get("top3") or 0),
                     "answered": int(r["answered"]), "radius_miles": r.get("radius_miles")})
    return {"rows": rows} if rows else None


RIVALS_SHOWN = 5


def rivals_for_report(competitors: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The roofers who held the top three most often, as Google Maps lists
    them: name, how many spots, rating, reviews. Nothing said about them
    beyond that. Names run through the copy rules like any text from outside;
    one that would trip them is left out rather than block the report."""
    out = []
    for c in competitors:
        name = _clean(str(c.get("title") or ""))
        if not name:
            continue
        rating = c.get("rating")
        out.append({"name": name, "spots": int(c.get("spots") or 0),
                    "rating": round(float(rating), 1) if isinstance(rating, (int, float)) else None,
                    "reviews": int(c["reviews"]) if isinstance(c.get("reviews"), (int, float)) else None})
        if len(out) == RIVALS_SHOWN:
            break
    return out


def reach_snapshot(reach: Mapping[str, Any], *, map_path: str | None,
                   with_rivals: bool = False) -> dict[str, Any] | None:
    """The Maps grid for the report: every spot and its position. Other
    roofers are named only when a person ticks that for this report
    (with_rivals). Pins sit on the map by the same projection the console
    uses; without a map they fall back to rows and columns."""
    from app.tools import staticmap

    if reach.get("status") != "done" or not reach.get("points"):
        return None
    size = int(reach.get("size") or 0)
    middle = (size - 1) // 2
    center = reach.get("center") or {}
    zoom = (reach.get("map") or {}).get("zoom")
    on_map = bool(map_path and zoom and center.get("lat") is not None)
    pins = []
    for pt in sorted(reach["points"], key=lambda p: (p.get("row", 0), p.get("col", 0))):
        rank = pt.get("rank")
        if pt.get("empty"):
            label, band = "", "empty"
        elif pt.get("error"):
            label, band = "?", "none"
        elif rank is None:
            label, band = "20+", "miss"
        else:
            label, band = str(rank), "good" if rank <= 3 else "ok" if rank <= 10 else "poor"
        pin = {"row": pt.get("row", 0), "col": pt.get("col", 0), "label": label, "band": band,
               "center": pt.get("row") == middle and pt.get("col") == middle}
        if on_map:
            pin["x"], pin["y"] = staticmap.offset((center["lat"], center["lng"]), pt["lat"], pt["lng"], zoom)
        pins.append(pin)
    answered = int(reach.get("answered") or 0)
    found = int(reach.get("found") or 0)
    return {"keyword": str(reach.get("keyword") or ""), "radius_miles": reach.get("radius_miles"),
            "size": size, "answered": answered, "top3": int(reach.get("top3") or 0), "found": found,
            "missing": max(0, answered - found), "pins": pins, "map_path": map_path if on_map else None,
            "center_on": reach.get("center_on") or "business", "center_label": reach.get("center_label") or "",
            "empty": int(reach.get("empty") or 0),
            "rivals": rivals_for_report(list(reach.get("competitors") or [])) if with_rivals else []}


def reviews_snapshot(prospect: Mapping[str, Any], reach: Mapping[str, Any], *,
                     with_names: bool = False) -> dict[str, Any] | None:
    """Their Google reviews next to the roofers who held the Maps top three
    most often in Local reach. None without both sides to compare."""
    from datetime import datetime, timezone

    count = prospect.get("review_count")
    others = [c for c in reach.get("competitors") or [] if isinstance(c.get("reviews"), (int, float))]
    if not isinstance(count, (int, float)) or reach.get("status") != "done" or not others:
        return None
    newest = prospect.get("latest_review_at")
    days = None
    if hasattr(newest, "tzinfo"):
        newest = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
        days = max(0, (datetime.now(timezone.utc) - newest).days)
    rows = []
    for c in others[:RIVALS_SHOWN]:
        name = _clean(str(c.get("title") or "")) if with_names else ""
        if with_names and not name:
            continue
        rating = c.get("rating")
        rows.append({"name": name or "", "reviews": int(c["reviews"]),
                     "rating": round(float(rating), 1) if isinstance(rating, (int, float)) else None})
    if not rows:
        return None
    rating = prospect.get("rating")
    return {"you": {"reviews": int(count), "newest_days": days,
                    "rating": round(float(rating), 1) if isinstance(rating, (int, float)) else None},
            "others": rows, "average": round(sum(r["reviews"] for r in rows) / len(rows))}


def demand_snapshot(found: Mapping[str, Any], *, town: str) -> dict[str, Any] | None:
    rows = [{"phrase": str(r["phrase"]), "monthly": int(r["monthly"])} for r in found.get("rows") or []]
    return {"town": town, "rows": rows, "top": int(found.get("top") or 0)} if rows else None


def ai_snapshot(found: Mapping[str, Any], *, with_names: bool = False) -> dict[str, Any] | None:
    """What each assistant said: whether it named them, and how many others
    it named. Those names only when a person ticks that for this report."""
    answers = []
    for a in found.get("answers") or []:
        names = [n for n in (_clean(x) for x in a.get("named") or []) if n]
        answers.append({"assistant": str(a.get("assistant") or ""), "named_you": bool(a.get("named_you")),
                        "others": len(names), "names": names if with_names else []})
    return {"question": _clean(str(found.get("question") or "")) or "", "answers": answers} if answers else None


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


def snapshot(audit: Mapping[str, Any], *, speed: bool, site_check: bool, positions: bool = False,
             reach: bool = False, rivals: bool = False, checks: list[Mapping[str, Any]] = (),
             city: str = "", reach_map_path: str | None = None, reviews: bool = False,
             prospect: Mapping[str, Any] | None = None, demand: Mapping[str, Any] | None = None,
             ai: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What report_extras should hold for the sections switched on. demand
    and ai are the lookups already made for this save, or None when their
    sections are off."""
    local = audit.get("local_reach") or {}
    return {
        "reviews": reviews_snapshot(prospect or {}, local, with_names=rivals) if reviews else None,
        "demand": demand_snapshot(demand, town=city) if demand else None,
        "ai": ai_snapshot(ai, with_names=rivals) if ai else None,
        "speed": speed_snapshot(audit.get("lighthouse") or {}) if speed else None,
        "site_check": site_check_snapshot(audit.get("technical") or {}) if site_check else None,
        "positions": positions_snapshot(list(checks), city=city, reach=local) if positions else None,
        "reach": reach_snapshot(local, map_path=reach_map_path, with_rivals=rivals) if reach else None,
    }
