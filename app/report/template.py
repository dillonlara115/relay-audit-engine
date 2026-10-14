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
  .ranks td.place {{ font-weight: 700; text-align: right; width: 42%; }}
  .gmap {{ position: relative; width: 100%; aspect-ratio: 1; border-radius: 6px; overflow: hidden;
          border: 3px solid var(--asphalt); margin: 10px 0 8px; background: #e8e2d6; }}
  .gmap img {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
  .pin {{ position: absolute; transform: translate(-50%, -50%); width: 9.5%; max-width: 38px; min-width: 22px;
          aspect-ratio: 1; border-radius: 50%; display: grid; place-items: center; font-family: 'PT Sans', sans-serif;
          font-weight: 700; font-size: 0.8rem; color: #fff; background: var(--c); border: 2px solid #fff;
          box-shadow: 0 1px 3px rgba(0,0,0,.35); }}
  .pin.miss {{ --c: #b9b1a4; color: var(--asphalt); }} .pin.none {{ --c: #d9d1c4; color: var(--asphalt); }}
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

  <h2>What we did</h2>
  <p>We searched for a roofer in {city} the way a homeowner with a leaking roof
  would, and looked at what they find. Then we walked through every step that
  homeowner would take to reach you.</p>

  {screenshot_block}

  <h2>Three things costing you booked jobs</h2>
  {findings_block}

  {positions_block}

  {reach_block}

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


def _positions_block(positions: Mapping[str, Any]) -> str:
    esc = html_escape.escape
    rows = "".join(f'<tr><td>{esc(r["search"])}</td><td class="place">{esc(r["place"])}</td></tr>'
                   for r in positions.get("rows") or [])
    return ("<h2>Where you rank on Google</h2>\n  <p>Where your business came up when we searched the way "
            "homeowners near you do.</p>\n  " f'<table class="ranks">{rows}</table>')


def _reach_block(reach: Mapping[str, Any], map_url: str | None) -> str:
    esc = html_escape.escape
    pins = reach.get("pins") or []
    answered, top3 = int(reach.get("answered") or 0), int(reach.get("top3") or 0)
    found, missing = int(reach.get("found") or 0), int(reach.get("missing") or 0)

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
    key = ('<div class="key"><span><i class="good"></i>Top 3</span><span><i class="ok"></i>4 to 10</span>'
           '<span><i class="poor"></i>11 to 20</span><span><i style="--c: #b9b1a4"></i>Not in the first 20</span>'
           '<span>Ringed: your business</span></div>')
    summary = (f"<p>You came up in the top three at {top3} of the {answered} spots, and somewhere in the first 20 "
               f"at {found}." + (f" At {missing} spots you didn't come up at all." if missing else "") + "</p>")
    return ("<h2>Where you show up in Google Maps</h2>\n  "
            f"<p>We searched \u201c{esc(reach.get('keyword') or '')}\u201d on Google Maps from {answered} spots up to "
            f"{esc(str(reach.get('radius_miles') or ''))} miles around your business, the way homeowners across "
            "your area would. Each circle is where you came up at that spot.</p>\n  "
            + board + key + summary)
