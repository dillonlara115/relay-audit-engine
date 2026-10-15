"""The one-page report. Brand tokens, outcome language, mobile first.

A pure function from PublicReport to HTML. No template engine: the whole page
is one string with named slots, which keeps the em-dash build test a plain scan
of this file and the escaping visible at the call site.
"""

from __future__ import annotations

import html as html_escape
from typing import Any, Mapping

from app.report.data import PublicReport

# Brand tokens, verbatim from CLAUDE.md.
ASPHALT = "#16120E"
CHALK = "#ECE6DC"
SAFETY_ORANGE = "#F25C1F"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{business_name}: what a homeowner finds</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="256x256" href="/static/relay-mark.png">
<link rel="apple-touch-icon" href="/static/relay-mark.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=PT+Sans:wght@400;700&family=Work+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --asphalt: #16120E;
    --chalk: #ECE6DC;
    --orange: #F25C1F;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: var(--chalk); color: var(--asphalt);
    font-family: 'Work Sans', sans-serif; font-size: 16px; line-height: 1.55;
  }}
  main {{ max-width: 680px; margin: 0 auto; padding: 24px 20px 64px; }}
  h1, h2 {{ font-family: 'PT Sans', sans-serif; letter-spacing: normal; font-weight: 700; }}
  h1 {{ font-size: 2.1rem; line-height: 1.1; margin: 8px 0 4px; }}
  h2 {{ font-size: 1.35rem; margin: 40px 0 12px; color: var(--asphalt); }}
  .kicker {{
    color: var(--orange); font-family: 'PT Sans', sans-serif; font-weight: 700;
    font-size: 1rem; letter-spacing: 0.08em; text-transform: uppercase;
  }}
  .city {{ font-size: 1rem; opacity: 0.75; margin-bottom: 28px; }}
  p {{ margin: 0 0 14px; }}
  .shot {{
    width: 100%; border: 3px solid var(--asphalt); border-radius: 6px;
    margin: 8px 0 4px; display: block;
  }}
  .shot-caption {{ font-size: 0.85rem; opacity: 0.7; margin-bottom: 8px; }}
  .finding {{
    background: #fff; border-left: 6px solid var(--orange);
    border-radius: 6px; padding: 18px 18px 12px; margin: 0 0 18px;
  }}
  .finding h3 {{
    font-family: 'PT Sans', sans-serif; font-size: 1.15rem; margin-bottom: 8px;
  }}
  .finding .fix {{ font-weight: 600; }}
  .limit {{ font-size: 0.95rem; opacity: 0.8; margin-top: 18px; }}
  .ask {{
    background: var(--asphalt); color: var(--chalk);
    border-radius: 6px; padding: 22px 20px; margin-top: 36px;
  }}
  .ask a {{ color: var(--orange); font-weight: 600; }}
  a {{ color: var(--orange); }}
  footer {{ margin-top: 40px; font-size: 0.85rem; opacity: 0.6; }}
  .rings {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 14px 0 18px; }}
  .ring-wrap {{ text-align: center; font-size: 0.95rem; font-weight: 600; }}
  .ring {{
    width: 76px; height: 76px; margin: 0 auto 6px; border-radius: 50%;
    display: grid; place-items: center;
    background: conic-gradient(var(--c) calc(var(--v) * 1%), #d9d1c4 0);
  }}
  .ring span {{
    width: 62px; height: 62px; border-radius: 50%; background: var(--chalk);
    display: grid; place-items: center; font-family: 'PT Sans', sans-serif;
    font-weight: 700; font-size: 1.3rem; color: var(--c);
  }}
  .good {{ --c: #1f7a43; }} .ok {{ --c: #a86200; }} .poor {{ --c: #c0341d; }}
  .checks {{ background: #fff; border-radius: 6px; padding: 16px 18px 6px 36px; margin: 0 0 14px; }}
  .checks li {{ margin: 0 0 10px; }}
  .checks ul {{ margin: 6px 0 4px 2px; padding-left: 18px; font-size: 0.95rem; }}
  .checks ul li {{ margin: 0 0 4px; overflow-wrap: anywhere; }}
  .checks .more {{ opacity: 0.7; list-style: none; margin-left: -18px; }}
  .note {{ font-size: 0.95rem; opacity: 0.8; }}
  .ranks {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; margin: 0 0 14px; }}
  .ranks td {{ padding: 12px 14px; border-bottom: 1px solid #ece6dc; vertical-align: top; }}
  .ranks tr:last-child td {{ border-bottom: 0; }}
  .ranks td.place {{ font-weight: 700; text-align: right; width: 40%; }}
  .ranks th {{ text-align: left; font-size: 0.85rem; font-weight: 600; opacity: 0.7; padding: 10px 14px 6px; }}
  .ranks th.place {{ text-align: right; }}
  .ranks .where {{ font-size: 0.9rem; opacity: 0.75; }}
  .rivals td.place {{ width: auto; white-space: nowrap; }}
  .short {{ background: #fff; border-left: 6px solid var(--asphalt); border-radius: 6px; padding: 16px 18px 6px 36px; margin: 8px 0 8px; }}
  .short li {{ margin: 0 0 10px; }}
  .bars {{ background: #fff; border-radius: 6px; padding: 14px 16px 6px; margin: 0 0 14px; }}
  .bar {{ margin: 0 0 12px; }}
  .bar .who {{ display: flex; justify-content: space-between; gap: 10px; font-size: 0.95rem; }}
  .bar .track {{ height: 12px; background: #ece6dc; border-radius: 6px; margin-top: 4px; overflow: hidden; }}
  .bar .fill {{ height: 100%; width: var(--w); background: #8a8175; border-radius: 6px; }}
  .bar.you .fill {{ background: var(--orange); }}
  .bar.you .who {{ font-weight: 700; }}
  .asked {{ font-style: italic; }}
  .said {{ background: #fff; border-radius: 6px; padding: 14px 16px; margin: 0 0 10px; }}
  .said b {{ font-family: 'PT Sans', sans-serif; }}
  h3.sub {{ font-family: 'PT Sans', sans-serif; font-size: 1.1rem; margin: 22px 0 6px; }}
  .gmap {{ position: relative; width: 100%; aspect-ratio: 1; border-radius: 6px; overflow: hidden;
          border: 3px solid var(--asphalt); margin: 10px 0 8px; background: #e8e2d6; }}
  .gmap img {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
  .pin {{ position: absolute; transform: translate(-50%, -50%); width: 9.5%; max-width: 38px; min-width: 22px;
          aspect-ratio: 1; border-radius: 50%; display: grid; place-items: center; font-family: 'PT Sans', sans-serif;
          font-weight: 700; font-size: 0.8rem; color: #fff; background: var(--c); border: 2px solid #fff;
          box-shadow: 0 1px 3px rgba(0,0,0,.35); }}
  .pin.miss {{ --c: #b9b1a4; color: var(--asphalt); }} .pin.none {{ --c: #d9d1c4; color: var(--asphalt); }}
  .pin.empty {{ background: transparent; border: 2px dashed rgba(22,18,14,.45); box-shadow: none; }}
  .pin.here {{ box-shadow: 0 0 0 3px var(--asphalt), 0 1px 3px rgba(0,0,0,.35); }}
  .gridonly {{ display: grid; gap: 6px; margin: 10px 0 8px; }}
  .gridonly .pin {{ position: static; transform: none; width: auto; max-width: none; }}
  .key {{ display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.85rem; opacity: 0.8; margin-bottom: 12px; }}
  .key i {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--c); margin-right: 5px; }}
</style>
</head>
<body>
<main>
  <div class="kicker">Prepared for</div>
  <h1>{business_name}</h1>
  <div class="city">{city}</div>

  {short_block}

  <h2>What we did</h2>
  <p>We searched for a roofer in {city} the way a homeowner with a leaking roof
  would, and looked at what they find. Then we walked through every step that
  homeowner would take to reach you.</p>

  {screenshot_block}

  <h2>Three things costing you booked jobs</h2>
  {findings_block}

  {demand_block}

  {positions_block}

  {reach_block}

  {reviews_block}

  {ai_block}

  {speed_block}

  {site_check_block}

  <p class="limit">From the outside we can see whether the tools are in place to
  catch a lead. We cannot see how fast your team actually moves. That is the
  next thing worth measuring.</p>

  {competitor_block}

  <h2>Your number</h2>
  <p>We never guess at dollar figures. Your inquiries, your average job value,
  your close rate: put your own numbers into
  <a href="{calculator_url}">this calculator</a> and see what the gaps above
  cost in a month.</p>

  <div class="ask">
    <p>Everything here is yours to keep, whoever fixes it. If you want it
    applied, reply to the message that brought you here and we will lay out
    exactly what we would do. No deck, no pressure, no strings.</p>
  </div>

  <footer>Prepared by Relay for Roofers. This page is private to you and was
  not submitted to any search engine.</footer>
</main>
</body>
</html>"""

_FINDING = """<div class="finding">
  <h3>{ordinal}. {what_we_saw}</h3>
  <p>{what_it_means}</p>
  <p class="fix">{what_fixing_takes}</p>
</div>"""

_SCREENSHOT = """<h2>What we found</h2>
  <p>This is your homepage exactly as a homeowner sees it on a phone.</p>
  <img class="shot" src="{url}" alt="Your site on a phone">
  <div class="shot-caption">Captured during the review. Nothing was altered.</div>"""

_SPEED = """<h2>Google's own speed test</h2>
  <p>We ran your homepage through Google's free PageSpeed test on a phone{measured}. It is
  the same test anyone can run, and Google uses what it measures.</p>
  <div class="rings">{rings}</div>
  {timing}
  <p class="note"><a href="{url}">Run the same test yourself</a>. Results move a little from run to run.</p>"""

_RING = """<div class="ring-wrap"><div class="ring {band}" style="--v: {value}"><span>{value}</span></div>{label}</div>"""

_SITE_CHECK = """<h2>Every page, checked</h2>
  <p>We went through {pages} pages of your site looking for things that send a visitor away or keep
  a page out of Google.{health}</p>
  {lines}"""

_COMPETITOR = """<h2>What good looks like</h2>
  <p>{note}</p>"""


def render_report(report: PublicReport) -> str:
    esc = html_escape.escape
    findings = "\n".join(
        _FINDING.format(
            ordinal=f.ordinal,
            what_we_saw=esc(f.what_we_saw),
            what_it_means=esc(f.what_it_means),
            what_fixing_takes=esc(f.what_fixing_takes),
        )
        for f in report.findings
    )
    screenshot = (
        _SCREENSHOT.format(url=esc(report.screenshot_url, quote=True))
        if report.screenshot_url else ""
    )
    competitor = (
        _COMPETITOR.format(note=esc(report.competitor_note))
        if report.competitor_note else ""
    )
    return _PAGE.format(
        short_block=_short_block(report),
        demand_block=_demand_block(report.demand) if report.demand else "",
        reviews_block=_reviews_block(report.reviews) if report.reviews else "",
        ai_block=_ai_block(report.ai) if report.ai else "",
        positions_block=_positions_block(report.positions) if report.positions else "",
        reach_block=_reach_block(report.reach, report.reach_map_url) if report.reach else "",
        speed_block=_speed_block(report.speed) if report.speed else "",
        site_check_block=_site_check_block(report.site_check) if report.site_check else "",
        business_name=esc(report.business_name),
        city=esc(report.city or "your city"),
        screenshot_block=screenshot,
        findings_block=findings,
        competitor_block=competitor,
        calculator_url=esc(report.calculator_url, quote=True),
    )


def _band(value: float) -> str:
    return "good" if value >= 90 else "ok" if value >= 50 else "poor"


def _speed_block(speed: Mapping[str, Any]) -> str:
    esc = html_escape.escape
    rings = "".join(_RING.format(band=_band(r["value"]), value=int(round(r["value"])), label=esc(r["label"]))
                    for r in speed.get("ratings") or [])
    timing = ""
    if speed.get("main_content"):
        who = "for real visitors on phones" if speed.get("real_visitors") else "on a typical phone"
        timing = f"<p>The main part of your homepage took {esc(speed['main_content'])} to show up {who}.</p>"
    measured = f", {esc(speed['measured'])}" if speed.get("measured") else ""
    url = speed.get("test_url") or "https://pagespeed.web.dev/"
    return _SPEED.format(rings=rings, timing=timing, measured=measured, url=esc(url, quote=True))


def _site_check_block(check: Mapping[str, Any]) -> str:
    esc = html_escape.escape
    health = (f" Overall, the site rates {int(round(check['health']))} out of 100 on site health."
              if check.get("health") is not None else "")
    lines = check.get("lines") or []
    body = ('<ul class="checks">' + "".join(_check_line(line) for line in lines) + "</ul>"
            if lines else "<p>Nothing broken turned up. That part of the site is in good shape.</p>")
    return _SITE_CHECK.format(pages=int(check.get("pages") or 0), health=health, lines=body)


def _check_line(line: Any) -> str:
    """One problem, and under it the pages where it sits. A snapshot saved
    before items existed carries the line as plain text."""
    esc = html_escape.escape
    if isinstance(line, str):
        return f"<li>{esc(line)}</li>"
    items = line.get("items") or []
    sub = ""
    if items:
        rows = []
        for it in items:
            link = (f'<a href="{esc(it["href"], quote=True)}">{esc(it["link"])}</a>'
                    if it.get("href") else esc(it.get("link") or ""))
            rows.append(f"<li>{link}{(': ' + esc(it['note'])) if it.get('note') else ''}</li>")
        if line.get("more"):
            rows.append(f'<li class="more">and {int(line["more"])} more</li>')
        sub = "<ul>" + "".join(rows) + "</ul>"
    return f"<li>{esc(line.get('text') or '')}{sub}</li>"


def _nth(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _position_row(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """(what a homeowner searches, where they searched, where you came up)."""
    if "kind" not in row:   # saved before the wording moved here
        return (str(row.get("search") or ""), "", str(row.get("place") or "")) if row.get("search") else None
    kind, query, rank = row.get("kind"), str(row.get("query") or ""), row.get("rank")
    if kind == "maps":
        return (query, "Google Maps", f"{_nth(int(rank))} on the list" if rank else "Not in the top 3")
    if kind == "search":
        return (query, "Google search", f"{_nth(int(rank))} on the page" if rank
                else "Not on the first two pages")
    if kind == "grid":
        top3, answered = int(row.get("top3") or 0), int(row.get("answered") or 0)
        where = f"Google Maps, from {answered} spots within {row.get('radius_miles')} miles of you"
        place = (f"Top 3 at {top3} of {answered} spots" if top3
                 else f"Not in the top 3 at any of the {answered} spots")
        return (query, where, place)
    return None


def _positions_block(positions: Mapping[str, Any]) -> str:
    esc = html_escape.escape
    rows = [r for r in (_position_row(x) for x in positions.get("rows") or []) if r]
    body = "".join(
        f'<tr><td><b>\u201c{esc(q)}\u201d</b>' + (f'<br><span class="where">{esc(w)}</span>' if w else "")
        + f'</td><td class="place">{esc(p)}</td></tr>' for q, w, p in rows)
    return ("<h2>Where you show up on Google</h2>\n  <p>We searched for a roofer the way a homeowner near "
            "you would. Here is where your business came up.</p>\n  "
            '<table class="ranks"><thead><tr><th>What a homeowner searches</th><th class="place">Where you came up'
            f"</th></tr></thead><tbody>{body}</tbody></table>")


def _reach_block(reach: Mapping[str, Any], map_url: str | None) -> str:
    esc = html_escape.escape
    # A spot whose search failed is our gap, not theirs: drawn empty, like a
    # spot where Google lists nobody, so the grid keeps its shape.
    pins = [{**p, "band": "empty", "label": ""} if p.get("band") == "none" else p
            for p in reach.get("pins") or []]
    answered, top3 = int(reach.get("answered") or 0), int(reach.get("top3") or 0)
    found, missing = int(reach.get("found") or 0), int(reach.get("missing") or 0)
    lower = max(0, found - top3)
    town = reach.get("center_label") if reach.get("center_on") == "town" else ""
    place_ringed = f"the middle of {town}" if town else "your business"
    around = esc(town) if town else "your business"

    def pin(p: Mapping[str, Any], *, placed: bool) -> str:
        style = f' style="left: {p["x"]}%; top: {p["y"]}%"' if placed else ""
        here = " here" if p.get("center") else ""
        return f'<div class="pin {esc(p["band"])}{here}"{style}>{esc(p["label"])}</div>'

    if map_url and all("x" in p for p in pins):
        board = (f'<div class="gmap"><img src="{esc(map_url, quote=True)}" alt="Map of the area we searched" '
                 f'width="640" height="640">' + "".join(pin(p, placed=True) for p in pins) + "</div>")
    else:
        size = int(reach.get("size") or 1)
        board = (f'<div class="gridonly" style="grid-template-columns: repeat({size}, 1fr)">'
                 + "".join(pin(p, placed=False) for p in pins) + "</div>")
    key = ('<div class="key"><span><i class="good"></i>Top 3, where most people look</span>'
           '<span><i class="ok"></i>4th to 10th</span><span><i class="poor"></i>11th to 20th</span>'
           '<span><i style="--c: #b9b1a4"></i>Not in the first 20</span>'
           + ('<span><i style="--c: transparent; outline: 1px dashed rgba(22,18,14,.5)"></i>No roofers listed there</span>'
              if any(p.get("band") == "empty" for p in pins) else "")
           + f'<span>Circled: {esc(place_ringed)}</span></div>')
    parts = [f"At {top3} {'spot' if top3 == 1 else 'spots'}, you're in the top 3, where most homeowners look."
             if top3 else "At none of these spots are you in the top 3, where most homeowners look."]
    if lower:
        parts.append(f"At {lower}, you're further down the list.")
    if missing:
        # Past the first 20 is not absent: Sun Spirit Roofing was 34th.
        parts.append(f"At {missing}, you're not in the first 20 results.")
    rivals = reach.get("rivals") or []
    rival_block = ""
    if rivals:
        rows = "".join(
            f'<tr><td><b>{esc(r["name"])}</b></td>'
            f'<td class="place">{int(r["spots"])} of {answered}</td>'
            f'<td class="place">' + (f'{r["rating"]:.1f} stars' if r.get("rating") is not None else "")
            + (f'<br><span class="where">{int(r["reviews"])} reviews</span>' if r.get("reviews") is not None else "")
            + "</td></tr>" for r in rivals)
        rival_block = ("<h3 class=\"sub\">Who homeowners see instead</h3>\n  <p>These roofers showed up in the "
                       "top 3 most often across the same spots.</p>\n  "
                       '<table class="ranks rivals"><thead><tr><th>Roofer</th><th class="place">In the top 3 at</th>'
                       f'<th class="place">On Google</th></tr></thead><tbody>{rows}</tbody></table>')
    return ("<h2>Where you show up in Google Maps</h2>\n  "
            f"<p>Google Maps shows different roofers depending on where the person searching is standing. "
            f"So we searched \u201c{esc(reach.get('keyword') or '')}\u201d from spots all around {around}, up to "
            f"{esc(str(reach.get('radius_miles') or ''))} miles out. Each circle shows where you "
            "landed on the list from that spot.</p>\n  "
            + board + key + "<p>" + " ".join(parts) + "</p>" + rival_block)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _demand_block(d: Mapping[str, Any]) -> str:
    esc = html_escape.escape
    rows = "".join(f'<tr><td><b>\u201c{esc(r["phrase"])}\u201d</b></td><td class="place">{int(r["monthly"]):,} a month</td></tr>'
                   for r in d.get("rows") or [])
    town = esc(d.get("town") or "your area")
    return (f"<h2>How many people are looking</h2>\n  <p>Google counts at least {int(d.get('top') or 0):,} searches a "
            f"month for a roofer in {town}, from people typing it one way alone. Here are the ways people "
            "type it most, with Google's own monthly counts.</p>\n  "
            '<table class="ranks"><thead><tr><th>What people type</th><th class="place">Searches</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>")


def _reviews_block(r: Mapping[str, Any]) -> str:
    esc = html_escape.escape
    you, others = r.get("you") or {}, r.get("others") or []
    top = max([int(you.get("reviews") or 0)] + [int(o["reviews"]) for o in others]) or 1

    def bar(label: str, reviews: int, rating: Any, *, mine: bool) -> str:
        stars = f"{rating:.1f} stars, " if isinstance(rating, (int, float)) else ""
        return (f'<div class="bar{" you" if mine else ""}"><div class="who"><span>{esc(label)}</span>'
                f"<span>{stars}{_plural(reviews, 'review', 'reviews')}</span></div>"
                f'<div class="track"><div class="fill" style="--w: {max(2, round(100 * reviews / top))}%"></div></div></div>')

    bars = bar("You", int(you.get("reviews") or 0), you.get("rating"), mine=True) + "".join(
        bar(o.get("name") or "Another roofer near you", int(o["reviews"]), o.get("rating"), mine=False)
        for o in others)
    mine, avg = int(you.get("reviews") or 0), int(r.get("average") or 0)
    if mine >= avg:
        line = f"You have {_plural(mine, 'review', 'reviews')}, more than the {avg} the roofers around you average."
    else:
        line = (f"You have {_plural(mine, 'review', 'reviews')}. The roofers who show up most often in Google Maps "
                f"near you have {avg} on average.")
    if you.get("newest_days") is not None:
        d = int(you["newest_days"])
        line += " Your newest review is from today." if d == 0 else f" Your newest review is {_plural(d, 'day', 'days')} old."
    return ("<h2>How your reviews compare</h2>\n  <p>Homeowners read reviews before they call, and Google leans on "
            "them when it decides who to show. " + line + "</p>\n  " f'<div class="bars">{bars}</div>')


def _ai_block(a: Mapping[str, Any]) -> str:
    esc = html_escape.escape
    said = []
    for ans in a.get("answers") or []:
        who, others, names = esc(ans["assistant"]), int(ans.get("others") or 0), ans.get("names") or []
        verdict = "named you." if ans.get("named_you") else "didn't name you."
        if others:
            rest = (f" It named {_plural(others, 'other roofer', 'other roofers')}" +
                    (": " + ", ".join(esc(n) for n in names) + "." if names else "."))
        else:
            rest = ""
        said.append(f'<div class="said"><b>{who}</b> {verdict}{rest}</div>')
    return ("<h2>What AI assistants say</h2>\n  <p>More homeowners now ask an AI assistant who to call instead of "
            f'searching. We asked two of them the same question:</p>\n  <p class="asked">\u201c{esc(a.get("question") or "")}\u201d</p>\n  '
            + "".join(said))


def _short_block(report: PublicReport) -> str:
    """Three lines at the top, from what the report goes on to show. Nothing
    new is claimed here: each line repeats a figure from a section below, and
    the last is the finding a person put first. Left off when the report has
    nothing beyond its findings, where it would only repeat them."""
    esc = html_escape.escape
    seen: list[str] = []
    reach, positions = report.reach or {}, report.positions or {}
    if reach.get("answered"):
        top3, n = int(reach.get("top3") or 0), int(reach["answered"])
        seen.append(f"In Google Maps, you're in the top 3 at {top3} of {n} spots around you." if top3
                    else f"In Google Maps, you're not in the top 3 at any of the {n} spots we searched from.")
    else:
        maps = next((r for r in positions.get("rows") or [] if r.get("kind") == "maps"), None)
        if maps:
            seen.append(f"Searching \u201c{maps['query']}\u201d on Google Maps, you're "
                        + (f"{_nth(int(maps['rank']))} on the list." if maps.get("rank") else "not in the top 3."))
    if report.demand and report.demand.get("top"):
        seen.append(f"Google counts at least {int(report.demand['top']):,} searches a month for a roofer in "
                    f"{report.demand.get('town') or 'your area'}.")
    ai = report.ai or {}
    if ai.get("answers"):
        missed = [x["assistant"] for x in ai["answers"] if not x.get("named_you")]
        if missed:
            seen.append(f"{' and '.join(missed)} {'doesn' if len(missed) == 1 else 'don'}'t name you when asked for the best roofers near you.")
    if report.reviews and len(seen) < 2:
        you, avg = int((report.reviews.get("you") or {}).get("reviews") or 0), int(report.reviews.get("average") or 0)
        if you < avg:
            seen.append(f"You have {you} reviews; the roofers who show up most average {avg}.")
    if not seen:
        return ""
    lines = seen[:2]
    if report.findings:
        lines.append(f"The biggest thing costing you jobs: {report.findings[0].what_we_saw}")
    return ("<h2>The short version</h2>\n  <ul class=\"short\">" + "".join(f"<li>{esc(x)}</li>" for x in lines) + "</ul>")
