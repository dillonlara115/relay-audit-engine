"""The read only overview.

Everything visual here comes from app.console.views: the same shell, the same
sidebar, the same helpers. This module keeps only what makes it different,
which is that it renders no buttons and no forms.

It used to carry its own copy of the shell, the palette and the escaping. That
duplication is exactly how the dashboard ended up on a different session gate
than the console for two days, and how it kept the old centred layout after the
console moved to a sidebar. One layout, defined once.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.console.views import (
    SEGMENT_COLORS,
    chip,
    esc,
    score_headers,
    score_legend,
    shell,
    tiles,
)

__all__ = ["SEGMENT_COLORS", "render_overview", "render_batch"]


def _progress(done: int, total: int) -> str:
    pct = int((done / total) * 100) if total else 0
    return f'<div class="bar"><i style="width:{pct}%"></i></div>'


# ── Screen one: every scan ────────────────────────────────────────────────────


def render_overview(batches: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td><a href="/dashboard/{esc(b["batch_id"])}">{esc(b["batch_id"])}</a></td>'
        f'<td class="num">{b.get("total", 0)}</td>'
        f'<td class="num">{b.get("done", 0)}</td>'
        f'<td class="num">{b.get("running", 0)}</td>'
        f'<td class="num">{b.get("pending", 0)}</td>'
        f'<td class="num">{b.get("failed", 0)}</td>'
        f'<td>{_progress(b.get("done", 0), b.get("total", 0))}</td>'
        f'<td class="muted">{esc(b.get("latest") or "")}</td></tr>'
        for b in batches
    )
    totals = {
        "scans": len(batches),
        "checked": sum(b.get("done") or 0 for b in batches),
        "in progress": sum((b.get("running") or 0) + (b.get("pending") or 0)
                           for b in batches),
    }
    body = (
        '<div class="topbar"><h1>Overview</h1></div>'
        '<p class="lede">Every scan from the last two weeks and how far each one '
        'got. This screen only reads: to start something, use '
        '<a href="/console">Start a scan</a>.</p>'
        + tiles([("scans", totals["scans"]), ("websites checked", totals["checked"]),
                 ("still in progress", totals["in progress"])])
        + "<h2>Scans</h2><table><tr><th>Scan</th><th>Companies</th><th>Checked</th>"
        "<th>Running</th><th>Waiting</th><th>Failed</th><th>Progress</th>"
        "<th>Last activity</th></tr>"
        + (rows or '<tr><td colspan="8" class="muted">No scans yet.</td></tr>')
        + "</table>"
    )
    return shell("Overview", body, active="dashboard")


# ── Screen two: one scan's call list ──────────────────────────────────────────


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
            tags.append('<span class="tag warn">incomplete</span>')
        findings = r.get("findings_status")
        report = ""
        if r.get("report_slug"):
            report = (f'<a href="/r/{esc(r["report_slug"])}" target="_blank" '
                      f'rel="noopener noreferrer">report</a>')
        elif findings:
            report = f'<span class="muted">{esc(findings)}</span>'
        table_rows.append(
            "<tr>"
            f'<td class="num">{esc(r.get("rank"))}</td>'
            f"<td>{esc(r.get('business_name'))}<br>"
            f"<span class='muted'>{esc(r.get('city') or '')}</span></td>"
            f"<td>{chip(r.get('segment'))}</td>"
            f'<td class="num">{scores.get("found", "")}</td>'
            f'<td class="num">{scores.get("chosen", "")}</td>'
            f'<td class="num">{scores.get("booked", "")}</td>'
            f'<td class="num">{scores.get("total", "")}</td>'
            f'<td class="tel">{esc(r.get("phone") or "")}</td>'
            f"<td>{' '.join(tags)}</td>"
            f"<td>{report}</td>"
            "</tr>"
        )

    body = (
        '<div class="lede"><a href="/dashboard">&larr; all scans</a></div>'
        '<div class="topbar"><h1>Who to call, in order</h1></div>'
        '<p class="lede">The best call is first: a company customers already '
        'find, whose leads slip away. '
        f'<span class="muted">Scan {esc(batch_id)}. Read only, so there are no '
        f'buttons here. Open it in <a href="/console/batches/{esc(batch_id)}">'
        'Results</a> to write talking points.</span></p>'
        + tiles(tile_pairs or [("companies checked", len(rows))])
        + score_legend()
        + "<h2>Call list</h2><table><tr><th>#</th><th>Company</th>"
        '<th>Opportunity<span class="sub">what kind of problem</span></th>'
        + score_headers()
        + '<th>Total<span class="sub">out of 100</span></th>'
        "<th>Phone</th><th></th><th>Report</th></tr>"
        + (
            "".join(table_rows)
            or '<tr><td colspan="10" class="muted">No websites checked yet.</td></tr>'
        )
        + "</table>"
    )
    return shell(f"Call list {batch_id}", body, active="dashboard")
