"""Console HTML. Pure functions from plain data to a page.

Placeholder substitution rather than str.format, because the CSS is full of
braces and escaping every one of them is how a template starts lying about
what it renders.
"""

from __future__ import annotations

import html as html_escape
import json
from typing import Any, Mapping, Sequence


# Segment chips, validated against the chalk surface with the palette
# validator: worst adjacent pair CVD delta E 12.8, normal vision 15.4. Identity
# is never colour alone, every chip carries its label.
SEGMENT_COLORS = {
    "Leaky Bucket": "#F25C1F",
    "Invisible Pro": "#1F6BF2",
    "Both Broken": "#6B4FA0",
    "Dialed": "#2E7D4F",
    "incomplete": "#7A746C",
}

_CSS = """
:root { --asphalt:#16120E; --chalk:#ECE6DC; --orange:#F25C1F; --line:#dcd5c8;
        --ink2:#5d564d; --panel:#fff;
        --shadow: 0 1px 2px rgba(22,18,14,.05), 0 6px 18px rgba(22,18,14,.06);
        --shadow-soft: 0 1px 2px rgba(22,18,14,.04), 0 3px 10px rgba(22,18,14,.05); }
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--chalk); color:var(--asphalt);
       font-family:'Work Sans',sans-serif; font-size:16px; line-height:1.55; }
a { color:var(--orange); text-decoration:none; }
a:hover { text-decoration:underline; }
h1,h2,h3,h4 { font-family:'Barlow Condensed',sans-serif; letter-spacing:.01em; font-weight:600; }
h1 { font-size:2rem; line-height:1.15; }
h2 { font-size:1.35rem; margin:34px 0 4px; }
h3 { font-size:1.1rem; margin:0 0 6px; }
.lede { color:var(--ink2); margin-bottom:20px; max-width:70ch; }
.muted { color:var(--ink2); }

/* shell */
.layout { display:flex; min-height:100vh; }
.side { width:232px; flex:0 0 232px; background:var(--asphalt); color:var(--chalk);
        padding:22px 16px; position:sticky; top:0; height:100vh; }
.side .brand { display:block; font-family:'Barlow Condensed',sans-serif; font-weight:600;
               font-size:1.35rem; letter-spacing:.04em; color:var(--orange);
               text-transform:uppercase; line-height:1.1; margin-bottom:4px; }
.side .brand:hover { text-decoration:none; opacity:.9; }
.side .tag { font-size:.78rem; color:#9a9186; margin-bottom:22px; display:block; }
.side nav a { display:block; padding:9px 12px; border-radius:7px; color:var(--chalk);
              font-size:.95rem; margin-bottom:3px; }
.side nav a:hover { background:#2a241d; text-decoration:none; }
.side nav a.on { background:var(--orange); color:#fff; font-weight:600; }
.side .foot { position:absolute; bottom:20px; left:16px; right:16px;
              font-size:.76rem; color:#8d857a; line-height:1.4; }
.main { flex:1; min-width:0; padding:26px 30px 70px; }
.topbar { display:flex; justify-content:space-between; align-items:baseline;
          gap:16px; margin-bottom:6px; flex-wrap:wrap; }

/* pieces */
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
        padding:18px 20px; margin-bottom:14px; box-shadow:var(--shadow); }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:14px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
         gap:10px; margin:14px 0; }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:11px;
        padding:13px 15px; box-shadow:var(--shadow-soft); }
.tile .n { font-family:'Barlow Condensed',sans-serif; font-weight:600; font-size:1.75rem;
           line-height:1; }
.tile .l { font-size:.79rem; color:var(--ink2); margin-top:3px; }

label { display:block; font-size:.85rem; color:var(--ink2); margin:12px 0 4px; font-weight:600; }
input[type=text], input[type=number], textarea, select {
  width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:9px;
  font-family:'Work Sans',sans-serif; font-size:16px; background:#fff;
  color:var(--asphalt); box-shadow:inset 0 1px 2px rgba(22,18,14,.04); }
input:focus, textarea:focus, select:focus {
  outline:none; border-color:var(--orange);
  box-shadow:0 0 0 3px rgba(242,92,31,.18); }
textarea { min-height:86px; resize:vertical; }
button { font-family:'Barlow Condensed',sans-serif; font-weight:600; letter-spacing:.03em;
         font-size:1.02rem; background:var(--orange); color:#fff; border:0; border-radius:9px;
         padding:10px 20px; cursor:pointer; margin-top:14px;
         box-shadow:0 1px 2px rgba(22,18,14,.12), 0 4px 10px rgba(242,92,31,.25); }
button:hover { filter:brightness(1.05); }
button.ghost, form.inline button { box-shadow:var(--shadow-soft); }
button.ghost { background:transparent; color:var(--asphalt); border:1px solid var(--line); }
button.danger { background:#8d2f16; }
form.inline { display:inline; }
form.inline button { margin-top:0; padding:5px 12px; font-size:.85rem; }
.hint { font-size:.83rem; color:var(--ink2); margin-top:6px; }

table { width:100%; border-collapse:separate; border-spacing:0;
        background:var(--panel); border:1px solid var(--line);
        border-radius:12px; overflow:hidden; box-shadow:var(--shadow); }
th { font-family:'Barlow Condensed',sans-serif; font-weight:600; text-align:left;
     font-size:.9rem; letter-spacing:.03em; padding:10px 12px;
     background:var(--asphalt); color:var(--chalk); }
th .sub { display:block; font-family:'Work Sans',sans-serif; font-weight:400;
          font-size:.71rem; color:#b6ada1; letter-spacing:0; margin-top:1px; }
td { padding:9px 12px; border-top:1px solid var(--line); vertical-align:top; font-size:.95rem; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.tel { white-space:nowrap; }
tr:hover td { background:#faf7f2; }
th[data-sort] { cursor:pointer; user-select:none; }
th[data-sort]:hover { color:var(--orange); }

.chip { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
.chip i { width:10px; height:10px; border-radius:3px; display:inline-block; }
.tag { font-size:.75rem; padding:1px 8px; border-radius:10px;
       background:var(--asphalt); color:var(--chalk); white-space:nowrap; }
.tag.warn { background:#8a5a00; }
.bar { height:8px; border-radius:4px; background:var(--line); overflow:hidden; min-width:80px; }
.bar i { display:block; height:100%; background:var(--orange); }

pre.log { background:var(--asphalt); color:#e8e2d6; border-radius:12px; padding:15px;
          box-shadow:var(--shadow);
          font-size:.85rem; line-height:1.5; max-height:440px; overflow:auto;
          white-space:pre-wrap; word-break:break-word; }
.status { display:inline-block; font-family:'Barlow Condensed',sans-serif; font-weight:600;
          letter-spacing:.03em; padding:2px 11px; border-radius:11px; background:var(--line); }
.status.running { background:var(--orange); color:#fff; }
.status.done { background:#2E7D4F; color:#fff; }
.status.failed { background:#8d2f16; color:#fff; }

.finding { border:1px solid var(--line); border-left:5px solid var(--orange);
           background:var(--panel); padding:15px 17px;
           border-radius:10px; margin-bottom:12px; box-shadow:var(--shadow-soft); }
.pass { color:#2E7D4F; font-weight:600; } .fail { color:#8d2f16; font-weight:600; }
.skip { color:var(--ink2); }
.banner { background:#fff3e6; border:1px solid #f2ddc4; border-left:5px solid var(--orange);
          padding:13px 15px; border-radius:10px; margin-bottom:14px; font-size:.95rem;
          box-shadow:var(--shadow-soft); }
.evidence-shot { max-width:340px; width:100%; border:1px solid var(--line);
                 border-radius:10px; display:block; box-shadow:var(--shadow); }
.evidence-cap { margin-top:6px; font-size:.85rem; }
abbr[title] { text-decoration:underline dotted; cursor:help; }
details.legend { background:var(--panel); border:1px solid var(--line);
  border-radius:12px; margin:14px 0; box-shadow:var(--shadow-soft); }
details.legend summary { cursor:pointer; padding:13px 17px;
  font-family:'Barlow Condensed',sans-serif; font-weight:600; font-size:1.05rem; }
details.legend .inner { padding:2px 17px 15px; }
details.legend h4 { font-size:1rem; margin:12px 0 2px; }
details.legend p { margin:0 0 5px; font-size:.93rem; }
.filterbar { margin-bottom:14px; }
.filterrow { display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr));
             gap:12px; align-items:end; }
.filterrow label { margin:0 0 4px; }
@media (max-width:860px) {
  .layout { display:block; }
  .side { width:auto; height:auto; position:static; }
  .side .foot { position:static; margin-top:16px; }
  .main { padding:20px 16px 60px; }
}
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
<div class="layout">
  <aside class="side">
    <a class="brand" href="/console">Relay<br>Audit Engine</a>
    <span class="tag">Find roofers worth calling</span>
    <nav>__NAV__</nav>
    <div class="foot">Everything here is internal. Reports are written by a
    model, checked by you, and never sent automatically.</div>
  </aside>
  <main class="main">__BODY__</main>
</div>
__SCRIPT__
</body>
</html>"""


def esc(value: Any) -> str:
    return html_escape.escape(str(value if value is not None else ""))


def _nav(active: str) -> str:
    """Plain labels. "Batches" and "Jobs" meant nothing to anyone who had not
    read the source, so the nav says what each screen is for."""
    items = [("/console", "console", "Start a scan"),
             ("/console/batches", "batches", "Results"),
             ("/console/jobs", "jobs", "Activity"),
             ("/dashboard", "dashboard", "Overview")]
    return "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for href, key, label in items
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
    "F": "Found: can a homeowner searching for a roofer find this company at all? Scored out of 30.",
    "C": "Chosen: once they find it, does the company look like a safe choice? Scored out of 30.",
    "B": "Booked: if someone wants to hire them, can they actually get through? Scored out of 40.",
}

SCORE_SUBS = {"F": "can they be found", "C": "do they look safe", "B": "can leads get through"}

SCORE_SORT_KEYS = {"F": "found", "C": "chosen", "B": "booked"}


def score_headers() -> str:
    """Three compact columns, each with a plain sub-label and a fuller
    explanation on hover. Click to sort."""
    return "".join(
        f'<th data-sort="{SCORE_SORT_KEYS[key]}"><abbr title="{esc(title)}">{key}</abbr>'
        f'<span class="sub">{esc(SCORE_SUBS[key])}</span></th>'
        for key, title in SCORE_TITLES.items()
    )


def score_legend(open_by_default: bool = False) -> str:
    """What the three numbers mean, written for someone who has never read the
    spec. Sits next to every table that shows them."""
    return f"""<details class="legend"{' open' if open_by_default else ''}>
  <summary>What do these numbers mean?</summary>
  <div class="inner">
    <p>Think of a homeowner whose roof is leaking. They go through three steps,
    and each company is scored out of 100 on how well it handles them.</p>
    <h4>Found, out of 30: can they be found at all?</h4>
    <p>Do they show up on Google with a healthy profile and recent reviews? Is
    the phone number on their site the same one on Google? Do they have pages
    for the towns they serve?</p>
    <h4>Chosen, out of 30: do they look like a safe choice?</h4>
    <p>Does the site work properly on a phone? Does it load quickly? Is the
    phone number easy to find and tap? Do they show real reviews, a warranty,
    proof they are licensed and insured?</p>
    <h4>Booked, out of 40: can a customer actually get through?</h4>
    <p>Can someone book a time online, or do they have to wait for a call back?
    Does the contact form actually work? If a call is missed, does anything
    follow up? This is worth the most because it is where jobs quietly go
    missing, and it is the part nobody else checks.</p>
    <h4>Opportunity types</h4>
    <p>{chip("Leaky Bucket")} Easy to find, but leads slip away. The best call
    on the list: they already have customers trying to reach them, and the fix
    is quick.</p>
    <p>{chip("Invisible Pro")} Ready to take work, but nobody finds them. Sell
    them visibility.</p>
    <p>{chip("Both Broken")} Weak on both sides. A bigger project, slower to
    close.</p>
    <p>{chip("Dialed")} Doing well already. Not a prospect, but worth asking who
    else they know.</p>
    <p>{chip("incomplete")} We could not finish checking them, usually because
    the site blocked us or a form only opens in a popup. We do not guess.</p>
  </div>
</details>"""


def status_pill(status: str) -> str:
    return f'<span class="status {esc(status)}">{esc(status)}</span>'


# ── Run screen ────────────────────────────────────────────────────────────────


def render_run(*, csrf: str, markets: Sequence[str], active_jobs: Sequence[Mapping[str, Any]],
               recent_batches: Sequence[Mapping[str, Any]]) -> str:
    # A datalist rather than a select: the four mapped metros are suggestions,
    # not a limit. resolve_market already understands "City, ST" for anywhere
    # else, so restricting the UI to a dropdown was the only thing stopping an
    # operator sweeping a market we have not enumerated yet.
    options = "".join(f'<option value="{esc(m)}, CO">' for m in markets)

    running = ""
    if active_jobs:
        rows = "".join(
            f'<tr><td><a href="/console/jobs/{esc(j["job_id"])}">{esc(j.get("label"))}</a></td>'
            f'<td>{status_pill(j.get("status", ""))}</td>'
            f'<td class="muted">{esc((j.get("log") or [{}])[-1].get("line", ""))}</td></tr>'
            for j in active_jobs
        )
        running = ("<h2>Happening right now</h2><table><tr><th>Job</th><th>Status</th>"
                   f"<th>Latest update</th></tr>{rows}</table>")

    batch_rows = "".join(
        f'<tr><td><a href="/console/batches/{esc(b["batch_id"])}">{esc(b["batch_id"])}</a></td>'
        f'<td class="num">{b.get("total", 0)}</td><td class="num">{b.get("done", 0)}</td>'
        f'<td><div class="bar"><i style="width:'
        f'{int((b.get("done", 0) / b["total"]) * 100) if b.get("total") else 0}%"></i></div></td>'
        f'<td class="muted">{esc(b.get("latest") or "")}</td></tr>'
        for b in recent_batches[:6]
    )

    body = f"""
<div class="topbar"><h1>Start a scan</h1></div>
<p class="lede">Pick a city and we will find the roofing companies there, screen
out the ones that are not a fit, then check each survivor's website the way a
customer would. Nothing is sent to anyone: this only looks.</p>

<details class="legend">
  <summary>What happens when I run this?</summary>
  <div class="inner">
    <p><strong>1. We find the companies.</strong> We search Google for roofing
    companies in the city you name, then screen each one out if they are not
    worth your time: commercial only, too few reviews, no real local address,
    or a storm chaser passing through.</p>
    <p><strong>2. We check their websites.</strong> Each company that survives
    gets its site opened on a simulated phone, timed for speed, and checked
    against about 30 things a customer would notice. We visit slowly and
    politely, and we never fill in or send anything.</p>
    <p><strong>3. We put them in call order.</strong> Not by score. The best
    call is a company that customers already find but whose leads slip away,
    because the problem is real and the fix is quick.</p>
    <p><strong>4. You get talking points.</strong> For the companies you choose,
    we draft the three problems costing them the most work, in plain language.
    You read and approve them before anything becomes a report you could send.</p>
  </div>
</details>

<div class="grid2">
  <div class="card">
    <h3>Find companies in a city</h3>
    <p class="muted">This step only looks them up and screens them. Websites are
    checked in the next step, so this is quick and cheap.</p>
    <form method="post" action="/console/sweep">
      {csrf_field(csrf)}
      <label for="market">Which city?</label>
      <input id="market" name="market" type="text" list="known-markets"
             value="Colorado Springs, CO" autocomplete="off"
             placeholder="Colorado Springs, CO">
      <datalist id="known-markets">{options}</datalist>
      <p class="hint">Type any city. The four suggestions have their surrounding
      towns mapped, so we can rule out a company based two counties away.
      Anywhere else works the same, except that one check says "not sure"
      instead of ruling someone out, so expect a few extra companies to come
      through.</p>
      <label for="limit">How many companies at most?</label>
      <input id="limit" type="number" name="limit" value="100" min="1" max="300">
      <button type="submit">Find companies</button>
    </form>
  </div>

  <div class="card">
    <h3>Or just describe what you want</h3>
    <p class="muted">Same work, but you describe the whole job in a sentence
    instead of clicking each step. It finds the companies, checks their
    websites, waits for that to finish, retries anything that got stuck, and
    hands back the call list.</p>
    <p class="muted"><strong>Worth using when</strong> the job takes several
    steps and you would rather not sit and watch it, especially a large city
    that runs for an hour. <strong>Use the buttons on the left instead</strong>
    when you know the one thing you want.</p>
    <p class="muted">It can only use the same steps you can. Every number it
    tells you came from an actual check, it cannot make one up, and it cannot
    contact anybody.</p>
    <form method="post" action="/console/agent">
      {csrf_field(csrf)}
      <label for="prompt">Describe the job in a sentence</label>
      <textarea id="prompt" name="prompt">Find roofing companies in Colorado Springs, check the websites of the 20 best ones, and show me who to call first.</textarea>
      <button type="submit">Run the coordinator</button>
    </form>
  </div>
</div>

{running}

<h2>Recent scans</h2>
<table><tr><th>Scan</th><th>Companies</th><th>Checked</th><th>Progress</th><th>Last activity</th></tr>
{batch_rows or '<tr><td colspan="5" class="muted">No scans yet. Start one above.</td></tr>'}</table>
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
            f"<th>Started</th></tr>{rows or '<tr><td colspan=4 class=muted>Nothing has run yet.</td></tr>'}</table>")
    return shell("Activity", body, active="jobs")


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
      // The arrow lives in its own element. The first version rewrote the
      // header's textContent, which flattened the <abbr> tooltips and the
      // sub-labels out of existence on the first click.
      table.querySelectorAll('th[data-sort] .arrow').forEach(function (a) { a.remove(); });
      var arrow = document.createElement('span');
      arrow.className = 'arrow';
      arrow.textContent = sortState.dir === 1 ? ' \u25B2' : ' \u25BC';
      th.appendChild(arrow);
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
                f'websites checked. <div class="bar" style="margin-top:6px">'
                f'<i style="width:{pct}%"></i></div></div>')

    body = f"""
<div class="lede"><a href="/console/batches">&larr; all scans</a></div>
<div class="topbar"><h1>Who to call, in order</h1></div>
<p class="lede">The best call is first. We rank by the kind of problem a company
has, not by score: a company customers already find, whose leads slip away, is a
faster and easier conversation than one that needs everything rebuilt.
<span class="muted">Scan {esc(batch_id)}.</span></p>
{live}
{tiles(tile_pairs or [("audits", len(rows))])}

<div class="card">
  <h3>Draft findings for the top prospects</h3>
  <p class="muted">The model picks three failures per prospect and writes the
  consequence in plain language. Every draft still needs a human to approve it.</p>
  <form method="post" action="/console/draft">
    {csrf_field(csrf)}
    <input type="hidden" name="batch_id" value="{esc(batch_id)}">
    <label for="top">How many companies?</label>
    <input id="top" type="number" name="top" value="10" min="1" max="40">
    <button type="submit">Write talking points</button>
  </form>
</div>

<h2>Call list</h2>
{score_legend()}

<div class="card filterbar">
  <div class="filterrow">
    <div>
      <label for="f-q">Search by name</label>
      <input id="f-q" type="text" placeholder="Company or city">
    </div>
    <div>
      <label for="f-segment">Opportunity type</label>
      <select id="f-segment">
        <option value="">Any type</option>
        <option value="Leaky Bucket">Leaky Bucket</option>
        <option value="Invisible Pro">Invisible Pro</option>
        <option value="Both Broken">Both Broken</option>
        <option value="Dialed">Dialed</option>
        <option value="incomplete">incomplete</option>
      </select>
    </div>
    <div>
      <label for="f-check">Show companies where</label>
      <select id="f-check">
        <option value="">Anything</option>
        {_check_filter_options(check_defs)}
      </select>
    </div>
    <div>
      <label for="f-status">is</label>
      <select id="f-status">
        <option value="">Any result</option>
        <option value="fail">a problem</option>
        <option value="pass">fine</option>
        <option value="skipped">not checked</option>
      </select>
    </div>
  </div>
  <p class="muted" id="f-count" style="margin-top:8px"></p>
  <p class="hint">For example, choose "C16: Footer copyright" and "a problem"
  to list every company whose website still shows an old copyright year. Click
  any column heading to sort by it.</p>
</div>

<table id="call-list"><thead><tr>
<th data-sort="rank">#</th><th data-sort="business">Company</th>
<th>Opportunity<span class="sub">what kind of problem</span></th>{score_headers()}
<th data-sort="total">Total<span class="sub">out of 100</span></th><th>Phone</th><th></th><th>Next step</th></tr></thead>
<tbody>
{"".join(table_rows) or '<tr><td colspan="10" class="muted">No websites checked yet.</td></tr>'}
</tbody></table>
{_BATCH_FILTER_SCRIPT}
"""
    return shell(f"Call list {batch_id}", body, active="batches")


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
    body = ('<div class="topbar"><h1>Results</h1></div>'
            '<p class="lede">Every scan from the last two weeks. Open one to see '
            'who to call.</p>'
            "<table><tr><th>Scan</th><th>Companies</th><th>Checked</th>"
            "<th>Running</th><th>Waiting</th><th>Progress</th><th>Last activity</th></tr>"
            f"{rows or '<tr><td colspan=7 class=muted>No scans yet.</td></tr>'}</table>")
    return shell("Results", body, active="batches")


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
        "found": "Can a homeowner searching for a roofer find them at all?",
        "chosen": "Once found, do they look like a safe choice?",
        "booked": "If someone wants to hire them, can they actually get through? "
                  "Worth the most, because this is where jobs quietly go missing.",
        "measurement": "Background information only. Not scored.",
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
                f"<table><tr><th>Code</th><th>What we looked at</th>"
                f"<th>Result</th><th>Points</th><th>What we found</th></tr>{rows}</table>")

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
            warn = ('<div class="banner">Read this before approving. Some wording may '
                    'describe how we found the problem rather than what the owner would '
                    'notice. He should hear what a customer experiences, never how we '
                    'measured it.</div>')
        if state == "draft":
            action = (f'<form method="post" action="/console/audits/{esc(audit_id)}/approve">'
                      f'{csrf_field(csrf)}<button type="submit">These look right</button>'
                      "</form>"
                      '<p class="hint">A person has to agree these are the right three '
                      "before a report can exist. Approving does not send or publish "
                      "anything.</p>")
        elif state == "approved" and not audit.get("report_slug"):
            action = (f'<form method="post" action="/console/audits/{esc(audit_id)}/publish">'
                      f'{csrf_field(csrf)}<button type="submit">Create the shareable report</button></form>')
        elif audit.get("report_slug"):
            action = (f'<p><a href="/r/{esc(audit["report_slug"])}" target="_blank" '
                      f'rel="noopener noreferrer">Open the report you can share</a></p>')
        else:
            action = ""
        findings_block = (f"<h2>Talking points <span class='tag'>{esc(state)}</span></h2>"
                          f"{warn}{cards}{action}")
    else:
        findings_block = (f"""<h2>Talking points</h2><div class="card">
<p class="muted">Nothing written yet. We will pick the three problems costing this
company the most work and explain each in plain language.</p>
<form method="post" action="/console/audits/{esc(audit_id)}/draft">{csrf_field(csrf)}
<button type="submit">Write talking points</button></form></div>""")

    def evidence_item(e: Mapping[str, Any]) -> str:
        url = e.get("url")
        kb = round((e.get("size_bytes") or 0) / 1024)
        caption = (f'<p class="muted evidence-cap">{esc(e.get("kind"))} '
                   f'&middot; {kb} KB captured during the audit</p>')
        if url and e.get("kind") == "screenshot":
            # The homepage exactly as the audit saw it, which is what the
            # Chosen and vision checks were reading. Click through for full size.
            return (f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                    f'<img class="evidence-shot" src="{esc(url)}" '
                    f'alt="Homepage as captured during the audit"></a>{caption}')
        if url:
            return (f'<p><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                    f'{esc(e.get("kind"))}</a>{caption}')
        problem = e.get("url_error")
        return (f'<p class="muted">{esc(e.get("kind"))}: {esc(e.get("gcs_path"))} '
                f'({kb} KB)' + (f' &middot; could not sign a link: {esc(problem)}'
                                if problem else "") + "</p>")

    shots = "".join(evidence_item(e) for e in evidence)

    # Google Business Profile. Places gives us googleMapsUri on every prospect,
    # which opens the public profile: the same thing a homeowner searching for
    # a roofer would land on, and where an operator checks reviews and hours.
    maps_uri = prospect.get("maps_uri")
    gbp_link = (
        f' &middot; <a href="{esc(maps_uri)}" target="_blank" rel="noopener noreferrer">'
        "Google Business Profile</a>"
    ) if maps_uri else ""

    warnings = ""
    if audit.get("crawl_error"):
        warnings += (f'<div class="banner"><strong>We had trouble reading this '
                    f'site.</strong> {esc(audit["crawl_error"])}. Some checks below '
                    f'may say "not checked" as a result.</div>')
    partial_sections = audit.get("partial_sections") or []
    if partial_sections:
        noun = "section" if len(partial_sections) == 1 else "sections"
        warnings += (f'<div class="banner"><strong>Incomplete check.</strong> We '
                    f'could not finish enough of the {esc(", ".join(partial_sections))} '
                    f'{noun} to score fairly. When Booked is affected we do not '
                    f'label the opportunity type at all, rather than guess.</div>')

    body = f"""
<div class="lede"><a href="/console/batches/{esc(audit.get("batch_id"))}">&larr; back to the call list</a></div>
<h1>{esc(prospect.get("business_name"))}</h1>
<div class="lede">{esc(prospect.get("city") or "")} &middot;
{esc(prospect.get("gbp_phone") or "")} &middot;
<a href="{esc(prospect.get("website_url") or "#")}" target="_blank" rel="noopener noreferrer">{esc(prospect.get("domain") or "no website")}</a>{gbp_link}</div>

{tiles([("found", scores.get("found", 0)), ("chosen", scores.get("chosen", 0)),
        ("booked", scores.get("booked", 0)), ("total", scores.get("total", 0)),
        ("band", audit.get("band") or ""), ("segment", audit.get("segment") or "incomplete")])}
{warnings}

<div class="card">
  <form class="inline" method="post" action="/console/audits/{esc(audit_id)}/reaudit">
    {csrf_field(csrf)}<button class="ghost" type="submit">Check this site again</button>
  </form>
  <form class="inline" method="post" action="/console/suppress"
        onsubmit="return confirm('Never contact this company again? This cannot be undone here.');">
    {csrf_field(csrf)}
    <input type="hidden" name="value" value="{esc(audit.get("prospect_id"))}">
    <input type="hidden" name="match_type" value="place_id">
    <input type="hidden" name="reason" value="requested">
    <button class="danger" type="submit">Never contact</button>
  </form>
</div>

{findings_block}
{section_table("found")}
{section_table("chosen")}
{section_table("booked")}
{section_table("measurement")}

<h2>What we saw</h2>
<div class="card">{shots or '<p class="muted">No screenshot was saved for this check.</p>'}</div>
"""
    return shell(prospect.get("business_name") or "Audit", body, active="batches")
