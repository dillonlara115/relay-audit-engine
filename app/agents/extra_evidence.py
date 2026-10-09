"""What Local reach and the Technical section add to a findings draft.

The diagnostician drafts from failed checks, and a finding must cite one. The
two console-run reports measure things the audit's checks do not, so each
becomes a check-shaped row of its own when it found a problem worth an owner's
time, and a line of ground truth when it found things working:

- R1, Maps reach: the grid of Maps searches around the business. F8 asks from
  one spot; this asks from dozens, so it can say which side of town a roofer
  disappears on.
- T1, the crawl: pages that are broken or that search cannot read.
- T2, phone speed retested: only when the audit's own speed checks passed and
  a later Lighthouse run disagrees. When they failed, the later run's numbers
  go into their notes instead, and when the later run says the site is now
  fast, those failures are dropped: the fresher measurement wins, so the owner
  is never told about a slow site he has since fixed.

Every number here was measured. Nothing is estimated, and nothing names
another business: the report never names a competitor unless a person writes
that line.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

REACH_CODE, CRAWL_CODE, SPEED_CODE = "R1", "T1", "T2"
SPEED_CHECKS = ("C3", "C4")

# Below this share of spots in the top three, reach is a finding.
REACH_WEAK = 0.6

# Crawl issues a homeowner or Google would trip over. Housekeeping (favicons,
# alt text, render-blocking files) stays on the console and out of the draft.
CRAWL_KEYS = ("is_4xx_code", "is_5xx_code", "broken_links", "broken_resources", "no_title",
              "duplicate_title", "duplicate_content", "no_h1_tag", "is_http", "low_content_rate",
              "no_description")

# Labels for the console's finding cards, so a person choosing three can see
# where a finding came from.
SOURCE_LABEL = {REACH_CODE: "Local reach", CRAWL_CODE: "Technical", SPEED_CODE: "Technical"}


def _sides(points: Sequence[Mapping[str, Any]], size: int) -> list[tuple[str, int, int]]:
    """(side, spots in the top three, spots searched) for each side of town."""
    mid = (size - 1) / 2
    halves = {"north": lambda p: p["row"] < mid, "south": lambda p: p["row"] > mid,
              "west": lambda p: p["col"] < mid, "east": lambda p: p["col"] > mid}
    out = []
    for side, inside in halves.items():
        spots = [p for p in points if not p.get("error") and inside(p)]
        if spots:
            out.append((side, sum(1 for p in spots if p.get("rank") and p["rank"] <= 3), len(spots)))
    return out


def reach_row(reach: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(a failure row, or None) and (a ground truth line, or None)."""
    if reach.get("status") != "done" or not reach.get("answered"):
        return None, None
    answered, top3, found = int(reach["answered"]), int(reach.get("top3") or 0), int(reach.get("found") or 0)
    keyword, radius, size = reach.get("keyword"), reach.get("radius_miles"), int(reach.get("size") or 0)
    where = f'searching "{keyword}" in Google Maps from {answered} spots up to {radius} miles around the business'
    if top3 / answered >= REACH_WEAK:
        return None, (f"Shows in the top three Maps results at {top3} of {answered} spots when {where}. "
                      "Do not say they are hard to find in Maps.")
    middle = next((p for p in reach.get("points") or []
                   if p.get("row") == (size - 1) // 2 and p.get("col") == (size - 1) // 2), None)
    at_home = ""
    if middle and not middle.get("error"):
        at_home = (f" At the business itself: number {middle['rank']}." if middle.get("rank")
                   else " At the business itself: not in the first 20.")
    sides = "; ".join(f"{side} {n} of {total}" for side, n, total in _sides(reach.get("points") or [], size))
    rivals = reach.get("competitors") or []
    rival_line = ""
    if rivals:
        lead = rivals[0]
        rival_line = (f" Other roofers hold the top three instead; the most common one does at "
                      f"{lead.get('spots')} of {answered} spots"
                      + (f" with {lead['reviews']} reviews" if lead.get("reviews") is not None else "")
                      + ". Never name another business.")
    note = (f"Measured {where}: in the top three at {top3}, listed in the first 20 at {found}, "
            f"missing from the first 20 at {answered - found}."
            + (f" Average position when listed: {reach['average_rank']}." if reach.get("average_rank") else "")
            + at_home
            + (f" Top three by side of town: {sides}." if sides else "")
            + rival_line)
    return {"code": REACH_CODE, "title": "Maps reach across the service area", "points": 3,
            "note": note}, None


def crawl_row(technical: Mapping[str, Any], *, text_unreliable: bool = False) -> dict[str, Any] | None:
    if technical.get("status") != "done":
        return None
    keys = set(CRAWL_KEYS)
    if text_unreliable:
        # A browser crawl, or a tool that holds text back, undercounts words.
        keys.discard("low_content_rate")
    issues = [i for i in technical.get("issues") or [] if i.get("key") in keys and i.get("count")]
    if not issues:
        return None
    listed = "; ".join(f"{i['label']}: {i['count']}" for i in issues)
    return {"code": CRAWL_CODE, "title": "Broken or unreadable pages across the site", "points": 2,
            "note": f"Crawled {technical.get('pages_crawled') or 'the'} pages of the site. Found: {listed}."}


def _speed_line(lh: Mapping[str, Any]) -> str:
    m = lh.get("metrics") or {}
    parts = []
    if isinstance(m.get("lcp_ms"), (int, float)):
        who = "real visitors" if m.get("lcp_source") == "field" else "a simulated phone"
        parts.append(f"main content took {m['lcp_ms'] / 1000:.1f} s to appear for {who}")
    if isinstance(m.get("speed_index_ms"), (int, float)):
        parts.append(f"the page looked complete after {m['speed_index_ms'] / 1000:.1f} s")
    # The 0 to 100 rating stays out: a report never quotes a score, ours or Google's.
    return "Google's own phone test: " + ", ".join(parts) + "." if parts else ""


def _speed_verdict(lh: Mapping[str, Any]) -> str | None:
    """'fast', 'slow' or None when the run does not say clearly."""
    perf = (lh.get("scores") or {}).get("performance")
    lcp = (lh.get("metrics") or {}).get("lcp_ms")
    if not isinstance(perf, (int, float)) or not isinstance(lcp, (int, float)):
        return None
    if perf >= 60 and lcp < 2500:
        return "fast"
    if perf < 50 or lcp > 4000:
        return "slow"
    return None


def _newer(a: Any, b: Any) -> bool:
    try:
        return a is not None and b is not None and a > b
    except TypeError:
        return False


def merge(audit: Mapping[str, Any], failures: Sequence[Mapping[str, Any]],
          passing: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """The failures and passing rows the diagnostician sees, with Local reach
    and Technical folded in. Also returns which sources were used."""
    failures = [dict(f) for f in failures]
    passing = [dict(p) for p in passing]
    used: list[str] = []

    row, truth = reach_row(audit.get("local_reach") or {})
    if row:
        failures.append(row)
    if truth:
        passing.append({"code": REACH_CODE, "title": "Maps reach across the service area", "note": truth})
    if row or truth:
        used.append("local_reach")

    from app.tools import stack

    tools = stack.found_on(audit)
    technical = audit.get("technical") or {}
    row = crawl_row(technical, text_unreliable=bool(technical.get("browser"))
                    or stack.any_of(tools, "delays_scripts"))
    if row:
        failures.append(row)
        used.append("crawl")

    lh = audit.get("lighthouse") or {}
    verdict, line = _speed_verdict(lh), _speed_line(lh)
    retested = _newer(lh.get("measured_at"), audit.get("finished_at") or audit.get("started_at"))
    # A tool that serves speed tests an optimised copy makes a simulated
    # "fast" worthless; only real visitors' timings may clear a failure.
    lab_only = (lh.get("metrics") or {}).get("lcp_source") != "field"
    if lab_only and stack.any_of(tools, "flatters_speed") and verdict == "fast":
        verdict = None
    failing_speed = [f for f in failures if f.get("code") in SPEED_CHECKS]
    if failing_speed and verdict == "fast" and retested:
        failures = [f for f in failures if f.get("code") not in SPEED_CHECKS]
        passing.append({"code": SPEED_CODE, "title": "Loads fast on a phone (retested)",
                        "note": line + " The site is fast now; do not call it slow."})
        used.append("lighthouse")
    elif failing_speed and line:
        for f in failures:
            if f.get("code") in SPEED_CHECKS:
                f["note"] = f"{f.get('note') or ''} {line}".strip()
        used.append("lighthouse")
    elif not failing_speed and verdict == "slow" and retested:
        failures.append({"code": SPEED_CODE, "title": "Slow on a phone (retested)", "points": 2, "note": line})
        used.append("lighthouse")

    failures.sort(key=lambda f: -(f.get("points") or 0))
    return failures, passing, used
