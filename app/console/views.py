"""Console HTML. Pure functions from plain data to a page.

Placeholder substitution rather than str.format, because the CSS is full of
braces and escaping every one of them is how a template starts lying about
what it renders.
"""

from __future__ import annotations

import html as html_escape
import json
from typing import Any, Mapping, Sequence

from app.report.dashboard import SEGMENT_COLORS

_CSS = """
:root { --asphalt:#16120E; --chalk:#ECE6DC; --orange:#F25C1F; --line:#e2dbcf; }
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--chalk); color:var(--asphalt);
       font-family:'Work Sans',sans-serif; font-size:16px; line-height:1.5; }
a { color:var(--orange); text-decoration:none; }
a:hover { text-decoration:underline; }
h1,h2,h3 { font-family:'Barlow Condensed',sans-serif; letter-spacing:.02em; font-weight:600; }
h1 { font-size:1.9rem; margin:2px 0; }
h2 { font-size:1.25rem; margin:30px 0 10px; }
h3 { font-size:1.05rem; margin:0 0 6px; }

header.top { background:var(--asphalt); color:var(--chalk); padding:12px 20px; }
header.top .inner { max-width:1120px; margin:0 auto; display:flex;
                    align-items:center; gap:20px; flex-wrap:wrap; }
header.top .brand { font-family:'Barlow Condensed',sans-serif; letter-spacing:.08em;
                    text-transform:uppercase; color:var(--orange);
                    text-decoration:none; }
header.top .brand:hover { text-decoration:none; opacity:.85; }
header.top nav a { color:var(--chalk); margin-right:16px; font-size:.95rem; }
header.top nav a.on { color:var(--orange); }
main { max-width:1120px; margin:0 auto; padding:22px 20px 72px; }
.sub { opacity:.7; margin-bottom:18px; }

.card { background:#fff; border-radius:8px; padding:18px; margin-bottom:14px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:14px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
         gap:10px; margin:12px 0; }
.tile { background:#fff; border-radius:6px; padding:12px 14px; }
.tile .n { font-family:'Barlow Condensed',sans-serif; font-size:1.6rem; line-height:1; }
.tile .l { font-size:.78rem; opacity:.65; margin-top:2px; }

label { display:block; font-size:.85rem; opacity:.7; margin:10px 0 4px; }
input[type=text], input[type=number], textarea, select {
  width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:6px;
  font-family:'Work Sans',sans-serif; font-size:16px; background:#fff; color:var(--asphalt); }
textarea { min-height:84px; resize:vertical; }
button { font-family:'Barlow Condensed',sans-serif; letter-spacing:.04em; font-size:1rem;
         background:var(--orange); color:#fff; border:0; border-radius:6px;
         padding:10px 18px; cursor:pointer; margin-top:12px; }
button.ghost { background:transparent; color:var(--asphalt);
               border:1px solid var(--line); }
button.danger { background:#8d2f16; }
button:disabled { opacity:.5; cursor:not-allowed; }
form.inline { display:inline; }
form.inline button { margin-top:0; padding:5px 11px; font-size:.85rem; }

table { width:100%; border-collapse:collapse; background:#fff;
        border-radius:8px; overflow:hidden; }
th { font-family:'Barlow Condensed',sans-serif; font-weight:400; text-align:left;
     font-size:.88rem; letter-spacing:.05em; padding:9px 11px;
     background:var(--asphalt); color:var(--chalk); }
td { padding:8px 11px; border-top:1px solid var(--line); vertical-align:top;
     font-size:.95rem; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.tel { white-space:nowrap; }
tr:hover td { background:#faf7f1; }

.chip { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
.chip i { width:10px; height:10px; border-radius:3px; display:inline-block; }
.tag { font-size:.75rem; padding:1px 7px; border-radius:10px;
       background:var(--asphalt); color:var(--chalk); white-space:nowrap; }
.tag.warn { background:#8a5a00; }
.tag.ok { background:#2E7D4F; }
.muted { opacity:.6; }
.bar { height:8px; border-radius:4px; background:var(--line); overflow:hidden; min-width:80px; }
.bar i { display:block; height:100%; background:var(--orange); }

pre.log { background:var(--asphalt); color:#e8e2d6; border-radius:8px; padding:14px;
          font-size:.85rem; line-height:1.45; max-height:460px; overflow:auto;
          white-space:pre-wrap; word-break:break-word; }
.status { display:inline-block; font-family:'Barlow Condensed',sans-serif;
          letter-spacing:.05em; padding:2px 10px; border-radius:10px;
          background:var(--line); }
.status.running { background:var(--orange); color:#fff; }
.status.done { background:#2E7D4F; color:#fff; }
.status.failed { background:#8d2f16; color:#fff; }

.filterbar { margin-bottom:14px; }
.filterrow { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
             gap:12px; align-items:end; }
.filterrow label { margin:0 0 4px; }
th[data-sort] { user-select:none; }
th[data-sort]:hover { color:var(--orange); }

.finding { border-left:5px solid var(--orange); background:#fff; padding:14px 16px;
           border-radius:6px; margin-bottom:12px; }
.checkrow td:first-child { font-family:'Barlow Condensed',sans-serif; }
.pass { color:#2E7D4F; } .fail { color:#8d2f16; } .skip { opacity:.55; }
abbr[title] { text-decoration:underline dotted; cursor:help; }
details.legend { background:#fff; border-radius:8px; padding:0; margin:14px 0; }
details.legend summary { cursor:pointer; padding:12px 16px;
  font-family:'Barlow Condensed',sans-serif; font-weight:600; font-size:1.05rem;
  letter-spacing:.03em; }
details.legend .inner { padding:2px 16px 14px; }
details.legend h4 { font-family:'Barlow Condensed',sans-serif; font-weight:600;
  font-size:1rem; margin:10px 0 2px; }
details.legend p { margin:0 0 4px; font-size:.92rem; }
details.legend .seg { margin-top:4px; }
.banner { background:#fff3e6; border-left:5px solid var(--orange);
          padding:12px 14px; border-radius:6px; margin-bottom:14px; font-size:.95rem; }
footer { margin-top:36px; font-size:.84rem; opacity:.6; }
"""

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600&family=Work+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head>
<body>
<header class="top"><div class="inner">
  <a class="brand" href="/console">Relay audit engine</a>
  <nav>__NAV__</nav>
</div></header>
<main>__BODY__</main>
<footer>Internal console. Findings are drafted by a model and approved by a
human before any report exists. Nothing here sends a message.</footer>
__SCRIPT__
</body>
</html>"""


def esc(value: Any) -> str:
    return html_escape.escape(str(value if value is not None else ""))


def _nav(active: str) -> str:
    items = [("/console", "Run"), ("/console/batches", "Batches"),
             ("/console/jobs", "Jobs"), ("/dashboard", "Dashboard")]
    return "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for href, label in items
        for key in [href.rsplit("/", 1)[-1] or "console"]
    )


def shell(title: str, body: str, *, active: str = "console", script: str = "") -> str:
    return (_SHELL
            .replace("__CSS__", _CSS)
            .replace("__TITLE__", esc(title))
            .replace("__NAV__", _nav(active))
            .replace("__BODY__", body)
            .replace("__SCRIPT__", script))


def csrf_field(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(token)}">'


def chip(segment: str | None) -> str:
    name = segment or "incomplete"
    color = SEGMENT_COLORS.get(name, SEGMENT_COLORS["incomplete"])
    return f'<span class="chip"><i style="background:{color}"></i>{esc(name)}</span>'


def tiles(pairs: Sequence[tuple[str, Any]]) -> str:
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="n">{esc(n)}</div><div class="l">{esc(l)}</div></div>'
        for l, n in pairs
    ) + "</div>"


SCORE_TITLES = {
    "F": "Found, out of 30. Can a homeowner with a leaking roof find this company at all?",
    "C": "Chosen, out of 30. Once found, do they look like the safe choice?",
    "B": "Booked, out of 40. If someone reaches out, is anything set up to catch the lead?",
}


SCORE_SORT_KEYS = {"F": "found", "C": "chosen", "B": "booked"}


def score_headers() -> str:
    """The three compact column headers, each explained on hover and clickable
    to sort the call list by that section's score."""
    return "".join(
        f'<th data-sort="{SCORE_SORT_KEYS[key]}"><abbr title="{esc(title)}">{key}</abbr></th>'
        for key, title in SCORE_TITLES.items()
    )


def score_legend(open_by_default: bool = False) -> str:
    """What Found, Chosen, Booked and the segments mean, in plain language.

    Lives next to every table that shows the numbers, because a score nobody
    can interpret is decoration.
    """
    return f"""<details class="legend"{' open' if open_by_default else ''}>
  <summary>How to read these scores</summary>
  <div class="inner">
    <p>Every audited company is scored out of 100 across three questions a
    homeowner answers in order:</p>
    <h4>Found (F), out of 30</h4>
    <p>Can a homeowner with a leaking roof find them at all? Google Business
    Profile health, review count and recency, phone number consistency, and
    whether their site shows up for their service area.</p>
    <h4>Chosen (C), out of 30</h4>
    <p>Once found, do they look like the safe choice? Mobile experience, page
    speed, visible phone number, trust signals like licensing, warranties and
    real customer reviews on the site.</p>
    <h4>Booked (B), out of 40</h4>
    <p>If someone raises a hand, is anything set up to catch it? Online
    booking, a working contact form, missed-call text-back, chat, a stated
    response time, an after-hours path. Weighted heaviest because it is the
    section nobody else audits and the one that loses jobs invisibly.</p>
    <h4>Segments</h4>
    <p class="seg">{chip("Leaky Bucket")} Easy to find, but little catches the
    lead. The best call on the list: demand already exists and the fix is
    quick.</p>
    <p class="seg">{chip("Invisible Pro")} Set up to convert, but nobody finds
    them. A visibility sale.</p>
    <p class="seg">{chip("Both Broken")} Weak on both sides. A full rebuild:
    real money, slower close.</p>
    <p class="seg">{chip("Dialed")} Strong on both. A referral partner, not a
    prospect.</p>
    <p class="seg">{chip("incomplete")} Booked could not be fully measured
    (site unreachable, form in a popup, bot protection), so no segment is
    claimed rather than guessing one.</p>
  </div>
</details>"""


def status_pill(status: str) -> str:
    return f'<span class="status {esc(status)}">{esc(status)}</span>'


# ── Run screen ────────────────────────────────────────────────────────────────


def render_run(*, csrf: str, markets: Sequence[str], active_jobs: Sequence[Mapping[str, Any]],
               recent_batches: Sequence[Mapping[str, Any]]) -> str:
    options = "".join(f'<option value="{esc(m)}">{esc(m)}</option>' for m in markets)

    running = ""
    if active_jobs:
        rows = "".join(
            f'<tr><td><a href="/console/jobs/{esc(j["job_id"])}">{esc(j.get("label"))}</a></td>'
            f'<td>{status_pill(j.get("status", ""))}</td>'
            f'<td class="muted">{esc((j.get("log") or [{}])[-1].get("line", ""))}</td></tr>'
            for j in active_jobs
        )
        running = ("<h2>Running now</h2><table><tr><th>Job</th><th>Status</th>"
                   f"<th>Latest</th></tr>{rows}</table>")

    batch_rows = "".join(
        f'<tr><td><a href="/console/batches/{esc(b["batch_id"])}">{esc(b["batch_id"])}</a></td>'
        f'<td class="num">{b.get("total", 0)}</td><td class="num">{b.get("done", 0)}</td>'
        f'<td><div class="bar"><i style="width:'
        f'{int((b.get("done", 0) / b["total"]) * 100) if b.get("total") else 0}%"></i></div></td>'
        f'<td class="muted">{esc(b.get("latest") or "")}</td></tr>'
        for b in recent_batches[:6]
    )

    body = f"""
<h1>Run a sweep</h1>
<div class="sub">Ingest a metro, screen it, audit the survivors. Every step is
resumable and every long job survives the worker that started it.</div>

<details class="legend">
  <summary>How a sweep works</summary>
  <div class="inner">
    <p><strong>1. Sweep.</strong> Google Places is searched for roofing
    contractors across the metro and its towns. Each one is screened by a fit
    gate: residential work, 25 or more reviews, a real local address, not a
    storm chaser.</p>
    <p><strong>2. Batch.</strong> The survivors are dispatched as a batch: each
    company's website is crawled politely, rendered on a simulated phone, speed
    tested, and scored across roughly 30 checks. See "How to read these scores"
    on any batch page for what the numbers mean.</p>
    <p><strong>3. Rank.</strong> The batch becomes a call list ordered by
    opportunity shape, not raw score: companies that are easy to find but lose
    the leads they attract rank first.</p>
    <p><strong>4. Findings and reports.</strong> A model drafts the three
    problems costing each company the most booked jobs. A human reads, approves
    and publishes a one-page report the contractor can be sent.</p>
  </div>
</details>

<div class="grid2">
  <div class="card">
    <h3>Sweep and gate a metro</h3>
    <p class="muted">Places search, then the fit gate. Nothing is crawled deeply yet.</p>
    <form method="post" action="/console/sweep">
      {csrf_field(csrf)}
      <label for="market">Metro</label>
      <select id="market" name="market">{options}</select>
      <label for="limit">Maximum prospects</label>
      <input id="limit" type="number" name="limit" value="100" min="1" max="300">
      <button type="submit">Start sweep</button>
    </form>
  </div>

  <div class="card">
    <h3>Ask the coordinator</h3>
    <p class="muted">An agent with the pipeline as its tools. It can sweep,
    dispatch, poll, resume and rank. It cannot invent a number.</p>
    <form method="post" action="/console/agent">
      {csrf_field(csrf)}
      <label for="prompt">What should it do</label>
      <textarea id="prompt" name="prompt">Sweep Colorado Springs, dispatch the top 20 for audit, wait for the batch to finish, then give me the ranked call list.</textarea>
      <button type="submit">Run the coordinator</button>
    </form>
  </div>
</div>

{running}

<h2>Recent batches</h2>
<table><tr><th>Batch</th><th>Tasks</th><th>Done</th><th>Progress</th><th>Last activity</th></tr>
{batch_rows or '<tr><td colspan="5" class="muted">No batches yet.</td></tr>'}</table>
"""
    return shell("Relay console", body, active="console")


# ── Job screen ────────────────────────────────────────────────────────────────


_POLL = """<script>
(function () {
  var id = document.body.dataset.job;
  var status = document.getElementById('job-status');
  var log = document.getElementById('job-log');
  var done = document.getElementById('job-done');
  if (!id) return;
  function tick() {
    fetch('/console/jobs/' + id + '.json', {credentials: 'same-origin'})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        status.textContent = j.status;
        status.className = 'status ' + j.status;
        log.textContent = (j.log || []).map(function (l) { return l.line; }).join('\\n');
        log.scrollTop = log.scrollHeight;
        if (j.status === 'done' || j.status === 'failed') {
          if (done) { done.style.display = 'block'; }
          window.location.reload();
          return;
        }
        setTimeout(tick, 2500);
      })
      .catch(function () { setTimeout(tick, 5000); });
  }
  tick();
})();
</script>"""


def render_job(job: Mapping[str, Any], *, csrf: str) -> str:
    status = job.get("status", "queued")
    lines = "\n".join(entry.get("line", "") for entry in (job.get("log") or []))
    result = job.get("result") or {}

    followups = ""
    if status == "done":
        if job.get("kind") == "sweep" and result.get("batch_id"):
            followups = f"""
<div class="card">
  <h3>Next: audit the survivors</h3>
  <p class="muted">{esc(result.get("eligible", 0))} prospects passed the gate.
  Dispatching fans them out over Pub/Sub, four at a time, politely.</p>
  <form method="post" action="/console/dispatch">
    {csrf_field(csrf)}
    <input type="hidden" name="batch_id" value="{esc(result.get("batch_id"))}">
    <input type="hidden" name="market" value="{esc((job.get("params") or {}).get("market"))}">
    <label for="dlimit">How many</label>
    <input id="dlimit" type="number" name="limit" value="40" min="1" max="200">
    <button type="submit">Dispatch audits</button>
  </form>
</div>"""
        elif result.get("batch_id"):
            followups = (f'<div class="card"><h3>Batch</h3>'
                         f'<p><a href="/console/batches/{esc(result["batch_id"])}">'
                         f'Open {esc(result["batch_id"])}</a></p></div>')

    answer = ""
    if result.get("answer"):
        answer = (f'<div class="card"><h3>What it reported</h3>'
                  f'<div style="white-space:pre-wrap">{esc(result["answer"])}</div></div>')

    error = ""
    if job.get("error"):
        error = f'<div class="banner"><strong>Failed.</strong> {esc(job["error"])}</div>'

    body = f"""
<div class="sub"><a href="/console">&larr; console</a></div>
<h1>{esc(job.get("label") or job.get("kind"))}</h1>
<div class="sub">Job {esc(job.get("job_id"))} &middot; {status_pill(status)}
<span id="job-status" style="display:none"></span></div>
{error}
<pre class="log" id="job-log">{esc(lines) or "Waiting for a worker to pick this up..."}</pre>
{answer}
{followups}
"""
    script = _POLL if status in ("queued", "running") else ""
    page = shell(f"Job {job.get('job_id')}", body, active="jobs", script=script)
    return page.replace("<body>", f'<body data-job="{esc(job.get("job_id"))}">')


def render_jobs(jobs_list: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td><a href="/console/jobs/{esc(j.get("job_id"))}">{esc(j.get("label"))}</a></td>'
        f'<td>{status_pill(j.get("status", ""))}</td>'
        f'<td class="muted">{esc(json.dumps(j.get("params") or {})[:80])}</td>'
        f'<td class="muted">{esc((j.get("created_at").strftime("%b %d %H:%M")) if j.get("created_at") else "")}</td>'
        "</tr>"
        for j in jobs_list
    )
    body = ("<h1>Jobs</h1><div class='sub'>Every long running operation, newest "
            "first.</div><table><tr><th>Job</th><th>Status</th><th>Params</th>"
            f"<th>Started</th></tr>{rows or '<tr><td colspan=4 class=muted>Nothing yet.</td></tr>'}</table>")
    return shell("Jobs", body, active="jobs")


# ── Batch screen, with actions ────────────────────────────────────────────────


# Vanilla JS, no build step: a search box, a segment filter, a check/status
# filter, and click-to-sort headers. Everything runs over the rows already in
# the page, so switching filters costs nothing server side.
_BATCH_FILTER_SCRIPT = """<script>
(function () {
  var table = document.getElementById('call-list');
  if (!table) return;
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.rows);
  var q = document.getElementById('f-q');
  var segSel = document.getElementById('f-segment');
  var checkSel = document.getElementById('f-check');
  var statusSel = document.getElementById('f-status');
  var countEl = document.getElementById('f-count');

  function apply() {
    var needle = (q.value || '').trim().toLowerCase();
    var seg = segSel.value;
    var code = checkSel.value;
    var status = statusSel.value;
    var shown = 0;
    rows.forEach(function (row) {
      var visible = true;
      if (needle && row.dataset.business.indexOf(needle) === -1) visible = false;
      if (visible && seg && row.dataset.segment !== seg) visible = false;
      if (visible && code) {
        var checks = JSON.parse(row.dataset.checks || '{}');
        var have = checks[code];
        if (status) {
          if (have !== status) visible = false;
        } else if (have === undefined) {
          visible = false;
        }
      }
      row.style.display = visible ? '' : 'none';
      if (visible) shown++;
    });
    if (countEl) countEl.textContent = shown + ' of ' + rows.length + ' shown';
  }

  [q, segSel, checkSel, statusSel].forEach(function (el) {
    el.addEventListener('input', apply);
    el.addEventListener('change', apply);
  });

  var sortState = {key: null, dir: 1};
  table.querySelectorAll('th[data-sort]').forEach(function (th) {
    th.style.cursor = 'pointer';
    th.addEventListener('click', function () {
      var key = th.dataset.sort;
      sortState.dir = sortState.key === key ? -sortState.dir : 1;
      sortState.key = key;
      table.querySelectorAll('th[data-sort]').forEach(function (h) {
        h.textContent = h.textContent.replace(/ [\u25B2\u25BC]$/, '');
      });
      th.textContent += sortState.dir === 1 ? ' \u25B2' : ' \u25BC';
      rows.sort(function (a, b) {
        var av = a.dataset['sort_' + key], bv = b.dataset['sort_' + key];
        var an = parseFloat(av), bn = parseFloat(bv);
        var cmp = (!isNaN(an) && !isNaN(bn)) ? (an - bn) : String(av).localeCompare(String(bv));
        return cmp * sortState.dir;
      });
      rows.forEach(function (row) { tbody.appendChild(row); });
    });
  });

  apply();
})();
</script>"""


def _check_filter_options(check_defs: Sequence[Mapping[str, Any]]) -> str:
    groups: dict[str, list[str]] = {}
    for d in check_defs:
        section = str(d.get("section") or "").title() or "Other"
        code, title = d.get("code"), d.get("title")
        groups.setdefault(section, []).append(
            f'<option value="{esc(code)}">{esc(code)}: {esc(title)}</option>'
        )
    return "".join(
        f'<optgroup label="{esc(section)}">{"".join(options)}</optgroup>'
        for section, options in groups.items()
    )


def render_batch(batch_id: str, rows: Sequence[Mapping[str, Any]],
                 segments: Mapping[str, int], check_defs: Sequence[Mapping[str, Any]] = (),
                 *, csrf: str, progress: Mapping[str, Any] | None = None) -> str:
    tile_pairs = [(name, segments.get(name, 0)) for name in
                  ("Leaky Bucket", "Invisible Pro", "Both Broken", "Dialed", "incomplete")
                  if segments.get(name)]

    table_rows = []
    for r in rows:
        scores = r.get("scores") or {}
        tags = []
        if r.get("incumbent_agency"):
            tags.append('<span class="tag">agency</span>')
        if r.get("partial"):
            tags.append('<span class="tag warn">partial</span>')

        state = r.get("findings_status")
        if r.get("report_slug"):
            action = f'<a href="/r/{esc(r["report_slug"])}">report</a>'
        elif state == "approved":
            action = (f'<form class="inline" method="post" action="/console/audits/'
                      f'{esc(r["audit_id"])}/publish">{csrf_field(csrf)}'
                      f'<button type="submit">Publish</button></form>')
        elif state == "draft":
            action = f'<a href="/console/audits/{esc(r["audit_id"])}">review draft</a>'
        else:
            action = f'<a href="/console/audits/{esc(r["audit_id"])}">open</a>'

        business_needle = esc(f'{r.get("business_name") or ""} {r.get("city") or ""}'.lower())
        checks_json = esc(json.dumps(r.get("checks") or {}, separators=(",", ":")))
        segment_value = esc(r.get("segment") or "incomplete")

        table_rows.append(
            f'<tr data-business="{business_needle}" data-segment="{segment_value}" '
            f'data-checks="{checks_json}" '
            f'data-sort_rank="{esc(r.get("rank"))}" '
            f'data-sort_business="{esc((r.get("business_name") or "").lower())}" '
            f'data-sort_found="{scores.get("found", -1)}" '
            f'data-sort_chosen="{scores.get("chosen", -1)}" '
            f'data-sort_booked="{scores.get("booked", -1)}" '
            f'data-sort_total="{scores.get("total", -1)}">'
            f'<td class="num">{esc(r.get("rank"))}</td>'
            f'<td><a href="/console/audits/{esc(r.get("audit_id"))}">'
            f'{esc(r.get("business_name"))}</a><br>'
            f'<span class="muted">{esc(r.get("city") or "")}</span></td>'
            f"<td>{chip(r.get('segment'))}</td>"
            f'<td class="num">{scores.get("found", "")}</td>'
            f'<td class="num">{scores.get("chosen", "")}</td>'
            f'<td class="num">{scores.get("booked", "")}</td>'
            f'<td class="num">{scores.get("total", "")}</td>'
            f'<td class="tel">{esc(r.get("phone") or "")}</td>'
            f"<td>{' '.join(tags)}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )

    live = ""
    if progress and progress.get("total"):
        pct = int((progress.get("done", 0) / progress["total"]) * 100)
        live = (f'<div class="banner">{progress.get("done", 0)} of {progress["total"]} '
                f'audits complete. <div class="bar" style="margin-top:6px">'
                f'<i style="width:{pct}%"></i></div></div>')

    body = f"""
<div class="sub"><a href="/console/batches">&larr; batches</a></div>
<h1>{esc(batch_id)}</h1>
<div class="sub">Ranked call order: segment priority first, emptiest Booked
bucket first within a segment.</div>
{live}
{tiles(tile_pairs or [("audits", len(rows))])}

<div class="card">
  <h3>Draft findings for the top prospects</h3>
  <p class="muted">The model picks three failures per prospect and writes the
  consequence in plain language. Every draft still needs a human to approve it.</p>
  <form method="post" action="/console/draft">
    {csrf_field(csrf)}
    <input type="hidden" name="batch_id" value="{esc(batch_id)}">
    <label for="top">How many prospects</label>
    <input id="top" type="number" name="top" value="10" min="1" max="40">
    <button type="submit">Draft findings</button>
  </form>
</div>

<h2>Call list</h2>
{score_legend()}

<div class="card filterbar">
  <div class="filterrow">
    <div>
      <label for="f-q">Search</label>
      <input id="f-q" type="text" placeholder="Business or city">
    </div>
    <div>
      <label for="f-segment">Segment</label>
      <select id="f-segment">
        <option value="">Any segment</option>
        <option value="Leaky Bucket">Leaky Bucket</option>
        <option value="Invisible Pro">Invisible Pro</option>
        <option value="Both Broken">Both Broken</option>
        <option value="Dialed">Dialed</option>
        <option value="incomplete">incomplete</option>
      </select>
    </div>
    <div>
      <label for="f-check">Check</label>
      <select id="f-check">
        <option value="">Any check</option>
        {_check_filter_options(check_defs)}
      </select>
    </div>
    <div>
      <label for="f-status">Result</label>
      <select id="f-status">
        <option value="">Any result</option>
        <option value="fail">Failed</option>
        <option value="pass">Passed</option>
        <option value="skipped">Skipped</option>
      </select>
    </div>
  </div>
  <p class="muted" id="f-count" style="margin-top:8px"></p>
  <p class="muted" style="margin-top:2px">Example: pick "C16: Footer copyright" and
  "Failed" to find businesses whose site still shows an old copyright year.
  Click a column header to sort by it.</p>
</div>

<table id="call-list"><thead><tr>
<th data-sort="rank">#</th><th data-sort="business">Business</th><th>Segment</th>{score_headers()}
<th data-sort="total">Total</th><th>Phone</th><th></th><th>Action</th></tr></thead>
<tbody>
{"".join(table_rows) or '<tr><td colspan="10" class="muted">No audits yet.</td></tr>'}
</tbody></table>
{_BATCH_FILTER_SCRIPT}
"""
    return shell(f"Batch {batch_id}", body, active="batches")


def render_batches(batches: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td><a href="/console/batches/{esc(b["batch_id"])}">{esc(b["batch_id"])}</a></td>'
        f'<td class="num">{b.get("total", 0)}</td><td class="num">{b.get("done", 0)}</td>'
        f'<td class="num">{b.get("running", 0)}</td><td class="num">{b.get("pending", 0)}</td>'
        f'<td><div class="bar"><i style="width:'
        f'{int((b.get("done", 0) / b["total"]) * 100) if b.get("total") else 0}%"></i></div></td>'
        f'<td class="muted">{esc(b.get("latest") or "")}</td></tr>'
        for b in batches
    )
    body = ("<h1>Batches</h1><div class='sub'>Every audit fan-out from the last "
            "two weeks.</div><table><tr><th>Batch</th><th>Tasks</th><th>Done</th>"
            "<th>Running</th><th>Pending</th><th>Progress</th><th>Last activity</th></tr>"
            f"{rows or '<tr><td colspan=7 class=muted>No batches yet.</td></tr>'}</table>")
    return shell("Batches", body, active="batches")


# ── Audit detail, where approval happens ──────────────────────────────────────

_STATUS_CLASS = {"pass": "pass", "fail": "fail", "skipped": "skip", "error": "fail"}


def render_audit(*, audit: Mapping[str, Any], prospect: Mapping[str, Any],
                 checks: Sequence[Mapping[str, Any]], definitions: Mapping[str, Any],
                 findings: Mapping[str, Any] | None, evidence: Sequence[Mapping[str, Any]],
                 csrf: str) -> str:
    scores = audit.get("scores") or {}
    audit_id = audit.get("audit_id") or audit.get("id") or ""

    sections: dict[str, list] = {"found": [], "chosen": [], "booked": [], "measurement": []}
    for check in sorted(checks, key=lambda c: definitions.get(c.get("code"), {}).get("sort_order", 0)):
        definition = definitions.get(check.get("code")) or {}
        section = definition.get("section")
        if section in sections:
            sections[section].append((check, definition))

    section_subs = {
        "found": "Can a homeowner with a leaking roof find this company at all?",
        "chosen": "Once found, do they look like the safe choice?",
        "booked": "If someone raises a hand, is anything set up to catch it? "
                  "Weighted heaviest: this is the section nobody else audits.",
        "measurement": "Diagnostics only. Recorded for context, worth no points.",
    }

    def section_table(name: str) -> str:
        rows = "".join(
            f'<tr class="checkrow"><td>{esc(c.get("code"))}</td>'
            f'<td>{esc(d.get("title"))}</td>'
            f'<td class="{_STATUS_CLASS.get(c.get("status"), "")}">{esc(c.get("status"))}</td>'
            f'<td class="num">{c.get("points_awarded", 0)}/{d.get("points", 0)}</td>'
            f'<td>{esc(c.get("note"))}</td></tr>'
            for c, d in sections[name]
        )
        if not rows:
            return ""
        return (f"<h2>{name.title()}</h2>"
                f'<div class="sub">{section_subs.get(name, "")}</div>'
                f"<table><tr><th>Code</th><th>Check</th>"
                f"<th>Result</th><th>Points</th><th>What we saw</th></tr>{rows}</table>")

    findings_block = ""
    if findings:
        state = findings.get("status")
        cards = "".join(
            f'<div class="finding"><h3>{esc(f.get("ordinal"))}. {esc(f.get("what_we_saw"))}</h3>'
            f'<p>{esc(f.get("what_it_means"))}</p>'
            f'<p><strong>{esc(f.get("what_fixing_takes"))}</strong></p>'
            + (f'<p class="muted">flagged: {esc(", ".join(f.get("mechanism_flags") or []))}</p>'
               if f.get("mechanism_flags") else "")
            + "</div>"
            for f in findings.get("findings") or []
        )
        warn = ""
        if findings.get("needs_review"):
            warn = ('<div class="banner">The model flagged wording that may name a '
                    'mechanism. Read it before approving: a contractor should never see '
                    'how we detected anything.</div>')
        if state == "draft":
            action = (f'<form method="post" action="/console/audits/{esc(audit_id)}/approve">'
                      f'{csrf_field(csrf)}<button type="submit">Approve these three</button>'
                      "</form>"
                      '<p class="muted" style="margin-top:8px">Approving is the human '
                      "selection the rules require. Nothing is published by approving.</p>")
        elif state == "approved" and not audit.get("report_slug"):
            action = (f'<form method="post" action="/console/audits/{esc(audit_id)}/publish">'
                      f'{csrf_field(csrf)}<button type="submit">Publish the report</button></form>')
        elif audit.get("report_slug"):
            action = (f'<p><a href="/r/{esc(audit["report_slug"])}">View the published '
                      f'report</a></p>')
        else:
            action = ""
        findings_block = (f"<h2>Findings <span class='tag'>{esc(state)}</span></h2>"
                          f"{warn}{cards}{action}")
    else:
        findings_block = (f"""<h2>Findings</h2><div class="card">
<p class="muted">No findings drafted yet.</p>
<form method="post" action="/console/audits/{esc(audit_id)}/draft">{csrf_field(csrf)}
<button type="submit">Draft three findings</button></form></div>""")

    shots = "".join(
        f'<p class="muted">{esc(e.get("kind"))}: {esc(e.get("gcs_path"))} '
        f'({esc(e.get("size_bytes"))} bytes)</p>'
        for e in evidence
    )

    warnings = ""
    if audit.get("crawl_error"):
        warnings += (f'<div class="banner"><strong>Crawl problem:</strong> '
                    f'{esc(audit["crawl_error"])}</div>')
    partial_sections = audit.get("partial_sections") or []
    if partial_sections:
        warnings += (f'<div class="banner"><strong>Partial:</strong> '
                    f'{esc(", ".join(partial_sections))} '
                    f'{"was" if len(partial_sections) == 1 else "were"} measured on '
                    f'fewer than 80% of its enabled points. Segment is withheld '
                    f'when Booked is the partial section.</div>')

    body = f"""
<div class="sub"><a href="/console/batches/{esc(audit.get("batch_id"))}">&larr; batch</a></div>
<h1>{esc(prospect.get("business_name"))}</h1>
<div class="sub">{esc(prospect.get("city") or "")} &middot;
{esc(prospect.get("gbp_phone") or "")} &middot;
<a href="{esc(prospect.get("website_url") or "#")}" target="_blank" rel="noopener noreferrer">{esc(prospect.get("domain") or "no website")}</a></div>

{tiles([("found", scores.get("found", 0)), ("chosen", scores.get("chosen", 0)),
        ("booked", scores.get("booked", 0)), ("total", scores.get("total", 0)),
        ("band", audit.get("band") or ""), ("segment", audit.get("segment") or "incomplete")])}
{warnings}

<div class="card">
  <form class="inline" method="post" action="/console/audits/{esc(audit_id)}/reaudit">
    {csrf_field(csrf)}<button class="ghost" type="submit">Re-audit now</button>
  </form>
  <form class="inline" method="post" action="/console/suppress"
        onsubmit="return confirm('Suppress this prospect permanently? This cannot be undone from here.');">
    {csrf_field(csrf)}
    <input type="hidden" name="value" value="{esc(audit.get("prospect_id"))}">
    <input type="hidden" name="match_type" value="place_id">
    <input type="hidden" name="reason" value="requested">
    <button class="danger" type="submit">Suppress permanently</button>
  </form>
</div>

{findings_block}
{section_table("found")}
{section_table("chosen")}
{section_table("booked")}
{section_table("measurement")}

<h2>Evidence</h2>
<div class="card">{shots or '<p class="muted">No evidence stored for this audit.</p>'}</div>
"""
    return shell(prospect.get("business_name") or "Audit", body, active="batches")
