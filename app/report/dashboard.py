"""The operator dashboard. Read-only, internal, two screens.

/dashboard            recent batches, one stat row each
/dashboard/{batch}    the ranked call list, grouped by segment

Internal means internal: scores, bands and segments appear here freely, which
is exactly why this page sits behind the operator gate and is never linked
from a public report. Nothing on it mutates anything.

Pure functions over plain dicts, same as the report template, and the same
brand tokens so the demo reads as one product.
"""

from __future__ import annotations

import html as html_escape
from typing import Any, Mapping, Sequence

# Segment chips, validated against the chalk surface (see dataviz check run):
# adjacent-pair CVD >= 10.7, normal-vision >= 15.4. Identity is never
# color-alone: every chip carries its label text.
SEGMENT_COLORS = {
    "Leaky Bucket": "#F25C1F",
    "Invisible Pro": "#1F6BF2",
    "Both Broken": "#6B4FA0",
    "Dialed": "#2E7D4F",
    "incomplete": "#7A746C",
}

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Staatliches&family=Barlow:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {{ --asphalt:#16120E; --chalk:#ECE6DC; --orange:#F25C1F; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:var(--chalk); color:var(--asphalt);
         font-family:'Barlow',sans-serif; font-size:16px; line-height:1.5; }}
  main {{ max-width:1080px; margin:0 auto; padding:24px 20px 64px; }}
  h1,h2 {{ font-family:'Staatliches',sans-serif; letter-spacing:.02em; }}
  h1 {{ font-size:1.9rem; margin:4px 0 2px; }}
  h2 {{ font-size:1.25rem; margin:32px 0 10px; }}
  .kicker {{ color:var(--orange); font-family:'Staatliches',sans-serif;
             letter-spacing:.08em; text-transform:uppercase; font-size:.95rem; }}
  .sub {{ opacity:.7; margin-bottom:22px; }}
  a {{ color:var(--orange); text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}

  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
            gap:10px; margin:14px 0 6px; }}
  .tile {{ background:#fff; border-radius:6px; padding:12px 14px; }}
  .tile .n {{ font-family:'Staatliches',sans-serif; font-size:1.7rem; line-height:1; }}
  .tile .l {{ font-size:.8rem; opacity:.65; margin-top:2px; }}

  table {{ width:100%; border-collapse:collapse; background:#fff;
           border-radius:6px; overflow:hidden; }}
  th {{ font-family:'Staatliches',sans-serif; font-weight:400; text-align:left;
        font-size:.9rem; letter-spacing:.05em; padding:10px 12px;
        background:var(--asphalt); color:var(--chalk); }}
  td {{ padding:9px 12px; border-top:1px solid #e2dbcf; vertical-align:top; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.tel {{ white-space:nowrap; }}
  tr:hover td {{ background:#faf7f1; }}

  .chip {{ display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }}
  .chip i {{ width:10px; height:10px; border-radius:3px; display:inline-block; }}
  .tag {{ font-size:.78rem; padding:1px 7px; border-radius:10px;
          background:var(--asphalt); color:var(--chalk); white-space:nowrap; }}
  .muted {{ opacity:.6; }}
  .bar {{ height:8px; border-radius:4px; background:#e2dbcf; overflow:hidden;
          min-width:90px; }}
  .bar i {{ display:block; height:100%; background:var(--orange); }}
  footer {{ margin-top:36px; font-size:.85rem; opacity:.6; }}
</style>
</head>
<body>
<main>
  <div class="kicker">Relay audit engine</div>
  {body}
  <footer>Internal. Read only. Scores and segments on this page never appear in
  anything a contractor sees.</footer>
</main>
</body>
</html>"""


def _esc(value: Any) -> str:
    return html_escape.escape(str(value if value is not None else ""))


def _chip(segment: str | None) -> str:
    name = segment or "incomplete"
    color = SEGMENT_COLORS.get(name, SEGMENT_COLORS["incomplete"])
    return (f'<span class="chip"><i style="background:{color}"></i>{_esc(name)}</span>')


def _tiles(pairs: Sequence[tuple[str, Any]]) -> str:
    tiles = "".join(
        f'<div class="tile"><div class="n">{_esc(n)}</div><div class="l">{_esc(l)}</div></div>'
        for l, n in pairs
    )
    return f'<div class="tiles">{tiles}</div>'


# ── Screen one: recent batches ────────────────────────────────────────────────


def render_overview(batches: Sequence[Mapping[str, Any]]) -> str:
    """batches: [{batch_id, total, done, running, pending, failed, audited_at?}]"""
    rows = []
    for b in batches:
        total = b.get("total") or 0
        done = b.get("done") or 0
        pct = int(done / total * 100) if total else 0
        rows.append(
            "<tr>"
            f'<td><a href="/dashboard/{_esc(b["batch_id"])}">{_esc(b["batch_id"])}</a></td>'
            f'<td class="num">{total}</td>'
            f'<td class="num">{done}</td>'
            f'<td class="num">{b.get("running") or 0}</td>'
            f'<td class="num">{b.get("pending") or 0}</td>'
            f'<td class="num">{b.get("failed") or 0}</td>'
            f'<td><div class="bar"><i style="width:{pct}%"></i></div></td>'
            f'<td class="muted">{_esc(b.get("latest") or "")}</td>'
            "</tr>"
        )
    totals = {
        "batches": len(batches),
        "audits": sum(b.get("done") or 0 for b in batches),
        "in flight": sum((b.get("running") or 0) + (b.get("pending") or 0) for b in batches),
    }
    body = (
        "<h1>Batches</h1>"
        '<div class="sub">Every audit fan-out from the last two weeks.</div>'
        + _tiles([("batches", totals["batches"]), ("audits complete", totals["audits"]),
                  ("in flight", totals["in flight"])])
        + "<h2>Recent</h2><table>"
        "<tr><th>Batch</th><th>Tasks</th><th>Done</th><th>Running</th>"
        "<th>Pending</th><th>Failed</th><th>Progress</th><th>Last activity</th></tr>"
        + "".join(rows or ["<tr><td colspan=8 class=muted>No batches yet. "
                           "Run a sweep and dispatch one.</td></tr>"])
        + "</table>"
    )
    return _SHELL.format(title="Relay: batches", body=body)


# ── Screen two: one batch's call list ─────────────────────────────────────────


def render_batch(
    batch_id: str,
    rows: Sequence[Mapping[str, Any]],
    segments: Mapping[str, int],
) -> str:
    """rows: ranked call list rows as dicts (rank, business_name, city, segment,
    scores, phone, partial, incumbent_agency, report_slug, findings_status)."""
    tile_pairs = [(name, segments.get(name, 0))
                  for name in ("Leaky Bucket", "Invisible Pro", "Both Broken",
                               "Dialed", "incomplete")
                  if segments.get(name)]

    table_rows = []
    for r in rows:
        scores = r.get("scores") or {}
        tags = []
        if r.get("incumbent_agency"):
            tags.append('<span class="tag">agency</span>')
        if r.get("partial"):
            tags.append('<span class="tag" style="background:#7A746C">partial</span>')
        findings = r.get("findings_status")
        report = ""
        if r.get("report_slug"):
            report = f'<a href="/r/{_esc(r["report_slug"])}">report</a>'
        elif findings:
            report = f'<span class="muted">{_esc(findings)}</span>'
        table_rows.append(
            "<tr>"
            f'<td class="num">{_esc(r.get("rank"))}</td>'
            f"<td>{_esc(r.get('business_name'))}<br><span class='muted'>{_esc(r.get('city') or '')}</span></td>"
            f"<td>{_chip(r.get('segment'))}</td>"
            f'<td class="num">{scores.get("found", "")}</td>'
            f'<td class="num">{scores.get("chosen", "")}</td>'
            f'<td class="num">{scores.get("booked", "")}</td>'
            f'<td class="num">{scores.get("total", "")}</td>'
            f'<td class="tel">{_esc(r.get("phone") or "")}</td>'
            f"<td>{' '.join(tags)}</td>"
            f"<td>{report}</td>"
            "</tr>"
        )

    body = (
        f'<div class="sub"><a href="/dashboard">&larr; all batches</a></div>'
        f"<h1>{_esc(batch_id)}</h1>"
        '<div class="sub">Ranked call order: segment priority first, emptiest '
        "Booked bucket first within a segment.</div>"
        + _tiles(tile_pairs or [("audits", len(rows))])
        + "<h2>Call list</h2><table>"
        "<tr><th>#</th><th>Business</th><th>Segment</th><th>F</th><th>C</th>"
        "<th>B</th><th>Total</th><th>Phone</th><th></th><th>Report</th></tr>"
        + "".join(table_rows or ["<tr><td colspan=10 class=muted>No audits in "
                                 "this batch yet.</td></tr>"])
        + "</table>"
    )
    return _SHELL.format(title=f"Relay: {batch_id}", body=body)
