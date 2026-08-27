"""The one-page report. Brand tokens, outcome language, mobile first.

A pure function from PublicReport to HTML. No template engine: the whole page
is one string with named slots, which keeps the em-dash build test a plain scan
of this file and the escaping visible at the call site.
"""

from __future__ import annotations

import html as html_escape

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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600&family=Work+Sans:wght@400;600&display=swap" rel="stylesheet">
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
  h1, h2 {{ font-family: 'Barlow Condensed', sans-serif; letter-spacing: 0.02em; font-weight: 600; }}
  h1 {{ font-size: 2.1rem; line-height: 1.1; margin: 8px 0 4px; }}
  h2 {{ font-size: 1.35rem; margin: 40px 0 12px; color: var(--asphalt); }}
  .kicker {{
    color: var(--orange); font-family: 'Barlow Condensed', sans-serif;
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
    font-family: 'Barlow Condensed', sans-serif; font-size: 1.15rem; margin-bottom: 8px;
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
  <img class="shot" src="{url}" alt="Your homepage on a phone">
  <div class="shot-caption">Captured during the review. Nothing was altered.</div>"""

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
        business_name=esc(report.business_name),
        city=esc(report.city or "your city"),
        screenshot_block=screenshot,
        findings_block=findings,
        competitor_block=competitor,
        calculator_url=esc(report.calculator_url, quote=True),
    )
