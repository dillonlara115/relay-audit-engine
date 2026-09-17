"""Console HTML. Pure functions from plain data to a page.

Screens render through Jinja templates in ./templates, one base layout and one
file per screen. Inside a template the HTML helpers arrive wrapped as Markup,
so nothing is marked safe by hand and everything else is escaped exactly once.
"""

from __future__ import annotations

import functools
import html as html_escape
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from app.console.auth import LOGIN_PATH


# Segment chips, validated with the palette validator: worst adjacent pair CVD
# delta E 12.8, normal vision 15.4. Identity is never colour alone, every chip
# carries its label.
#
# Re-measured against the lighter field. Every dot improved, and Leaky Bucket's
# orange went from 2.67:1 on the old beige, which was under the 3:1 floor for
# a non-text component, to 3.05:1 on the field and 3.32:1 on a white panel.
# Lightening the page fixed a contrast failure that had been sitting there.
SEGMENT_COLORS = {
    "Leaky Bucket": "#F25C1F",
    "Invisible Pro": "#1F6BF2",
    "Both Broken": "#6B4FA0",
    "Dialed": "#2E7D4F",
    "incomplete": "#7A746C",
}


TEMPLATES = Path(__file__).parent / "templates"

# Every screen renders through one base layout. The stylesheet is handed to
# the templates as an already-marked string rather than included as a
# template, so Jinja never has to parse a sheet full of braces.
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html", "svg"], default=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["mon_d"] = lambda dt: dt.strftime("%b %d") if dt else ""


def theme_css() -> str:
    """The one stylesheet, raw. The palette tests read it from here."""
    return (TEMPLATES / "_theme.css").read_text(encoding="utf-8")


def _theme_css_stripped() -> str:
    """The sheet without its comments, for the page that must say nothing."""
    return re.sub(r"/\*.*?\*/", "", theme_css(), flags=re.S)


# One vocabulary for the rail: Overview, Sweeps, Jobs. Icon names are sprite
# symbol ids without the i- prefix.
NAV_GROUPS = [
    ("Prospecting", [("/console", "overview", "Overview", "home"),
                     ("/console/batches", "batches", "Sweeps", "list")]),
    ("System", [("/console/jobs", "jobs", "Jobs", "activity")]),
]

# Older call sites still say active="console" or "dashboard".
_ACTIVE_ALIASES = {"console": "overview", "dashboard": "overview"}


def _markup(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Markup:
        return Markup(fn(*args, **kwargs))
    return wrapped


def icon(name: str) -> Markup:
    return Markup(f'<svg class="ic" aria-hidden="true"><use href="#i-{esc(name)}"></use></svg>')


def _render(template: str, **ctx: Any) -> str:
    """Render one screen with the shared context every template expects."""
    ctx.setdefault("theme_css", Markup(theme_css()))
    ctx.setdefault("badges", {})
    ctx.setdefault("nav_groups", NAV_GROUPS)
    ctx.setdefault("csrf", None)
    ctx.setdefault("script", Markup(""))
    ctx.setdefault("body_attrs", Markup(""))
    ctx.setdefault("notice", None)
    # Helpers build HTML with esc() inside them. Templates get them wrapped as
    # Markup so autoescape leaves the result alone; the Python callers still
    # on shell() get plain str, because str + Markup escapes the str and a
    # screen that concatenates its body would lose its own tags.
    ctx.setdefault("icon", icon)
    for name in ("csrf_field", "status_pill", "chip", "tiles", "progress_bar",
                 "scan_label", "score_headers", "score_legend", "contact_cell",
                 "outreach_cell"):
        ctx.setdefault(name, _markup(globals()[name]))
    active = ctx.get("active", "overview")
    ctx["active"] = _ACTIVE_ALIASES.get(active, active)
    return _env.get_template(template).render(**ctx)


def esc(value: Any) -> str:
    return html_escape.escape(str(value if value is not None else ""))


# Runs on every console page. A slow redirect after a sweep or dispatch left
# the button live, and a second click started a second identical job, spending
# real Places or Vertex quota twice. Disabling on submit does not cancel the
# submission in flight, only a second one; a form can still opt out with
# data-no-guard for the rare case where re-submitting is intentional (there is
# none yet, the escape hatch is here so one is not forced to fight this).
_SUBMIT_GUARD = """<script>
(function () {
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.hasAttribute('data-no-guard')) return;
    var button = form.querySelector('button[type="submit"], button:not([type])');
    if (!button || button.disabled) return;
    button.disabled = true;
    button.dataset.label = button.textContent;
    button.textContent = button.dataset.loading || 'Working\u2026';
  });
})();
</script>"""


_TABLE = re.compile(r"<table\b.*?</table>", re.S)


def _wrap_tables(body: str) -> str:
    """Every table scrolls inside its own box instead of taking the page with
    it. One point of application so no render function has to remember it."""
    return _TABLE.sub(lambda m: f'<div class="table-wrap">{m.group(0)}</div>', body)


def shell(title: str, body: str, *, active: str = "console", script: str = "") -> str:
    """Wrapper for screens not yet on their own template.

    The body is trusted HTML the caller built, wrapped the way the old shell
    did it. Each screen leaves this behind as it gets a template.
    """
    return _render("base.html", title=title, body=Markup(_wrap_tables(body)),
                   active=active, script=Markup(script))


def csrf_field(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(token)}">'


def chip(segment: str | None) -> str:
    name = segment or "incomplete"
    color = SEGMENT_COLORS.get(name, SEGMENT_COLORS["incomplete"])
    return f'<span class="chip"><i style="background:{color}"></i>{esc(name)}</span>'


def progress_bar(done: int, total: int) -> str:
    """A bar that turns green at 100%, so a glance across the scans table
    tells 'finished' from 'still running' without reading the numbers."""
    pct = int((done / total) * 100) if total else 0
    complete = " done" if total and done >= total else ""
    return f'<div class="bar{complete}"><i style="width:{pct}%"></i></div>'


def scan_label(batch: Mapping[str, Any]) -> str:
    """'Colorado Springs &middot; Aug 26' when we know the metro and the
    start date, the raw scan id otherwise. A batch built without a sweep
    behind it (the CLI, a smoke test) has neither on record, and the id is
    still there, just no longer the only thing to look at."""
    batch_id = batch.get("batch_id", "")
    market = batch.get("market")
    started = batch.get("started_at")
    if market and started:
        return (f'{esc(market)} <span class="muted">&middot; '
                f'{esc(started.strftime("%b %d"))}</span>')
    if market:
        return esc(market)
    return esc(batch_id)


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


_CONTACT_TAG = {"valid": "ok", "risky": "warn", "invalid": "bad", "unknown": "dim"}

# What the operator is told, not what the checker measured. "Shared mailbox" is
# the consequence; "role address" is the mechanism.
_CONTACT_LABEL = {
    "valid": "good", "risky": "check first", "invalid": "bad", "unknown": "unchecked",
}


def render_login(*, next_path: str = "/console", error: str | None = None) -> str:
    """The password prompt.

    Deliberately says nothing about what is behind it. Whoever is looking at
    this either knows already or has no business finding out, and the same page
    answers a trimmed report URL as answers a bookmark to the call list. So it
    renders on the bare shell, with the stylesheet stripped of its comments.
    """
    return _env.get_template("login.html").render(
        theme_css=Markup(_theme_css_stripped()), next_path=next_path,
        error=error, login_path=LOGIN_PATH)


def contact_cell(contacts: Sequence[Mapping[str, Any]]) -> str:
    """The best address we found, or an honest blank.

    A prospect with no address is not a failure and is not styled like one. It
    is a prospect whose site does not publish one, which is a thing to go and
    find by hand.
    """
    usable = [c for c in contacts if c.get("status") in ("valid", "risky", "unknown")]
    if not usable:
        return '<span class="muted">none on the site</span>'
    first = usable[0]
    status = str(first.get("status") or "unknown")
    extra = f'<br><span class="muted">+{len(usable) - 1} more</span>' if len(usable) > 1 else ""
    return (f'<span class="mail">{esc(first.get("email"))}</span> '
            f'<span class="tag {_CONTACT_TAG.get(status, "dim")}">'
            f'{esc(_CONTACT_LABEL.get(status, status))}</span>{extra}')


def outreach_cell(sequence: Mapping[str, Any] | None, *, prospect_id: str,
                  audit_id: str | None, csrf: str, can_start: bool) -> str:
    """Where this prospect sits in the four-touch sequence.

    The button records a touch an operator already sent by hand. It is not a
    send button and the route behind it transmits nothing.
    """
    from app import outreach

    state = ""
    show_button = can_start
    if sequence:
        seq = outreach.Sequence.from_dict(sequence)
        if seq.status == outreach.CLOSED:
            reason = seq.closed_reason or "finished"
            if seq.last_intent:
                reason = outreach.INTENT_LABELS.get(seq.last_intent, seq.last_intent)
            return f'<span class="muted">{esc(reason)}</span>'
        if seq.status == outreach.WAITING:
            return f'<span class="tag warn">{esc(outreach.park_reason(seq))}</span>'
        show_button = seq.is_open
        if seq.touch_count:
            due = seq.next_due_at.strftime("%b %d") if seq.next_due_at else ""
            state = (f'<span class="muted">{seq.touch_count} of '
                     f'{outreach.MAX_TOUCHES} sent{", next " + esc(due) if due else ""}</span><br>')

    if not show_button:
        return state or '<span class="muted">not started</span>'
    label = "I sent this" if state else "I sent the first one"
    audit_field = (f'<input type="hidden" name="audit_id" value="{esc(audit_id)}">'
                   if audit_id else "")
    return (f'{state}<form class="inline" method="post" '
            f'action="/console/outreach/{esc(prospect_id)}/log-touch">'
            f'{csrf_field(csrf)}{audit_field}'
            f'<button class="ghost" type="submit">{label}</button></form>')


# ── Run screen ────────────────────────────────────────────────────────────────


def render_run(*, csrf: str, markets: Sequence[str], active_jobs: Sequence[Mapping[str, Any]],
               recent_batches: Sequence[Mapping[str, Any]]) -> str:
    """The Overview: what is running, what has run, and the two ways to start."""
    recent = [dict(b) for b in recent_batches]

    def total(key: str) -> int:
        return sum(int(b.get(key) or 0) for b in recent)

    kpis = [
        ("Jobs running", len(active_jobs), "queued or running"),
        ("Sweeps", len(recent), "last two weeks"),
        ("Audits finished", total("done"), "across those sweeps"),
        ("Audits waiting", total("running") + total("pending"), "queued or running"),
        ("Audits failed", total("failed"), "worth a look"),
    ]
    jobs_vm = [{
        "job_id": j.get("job_id"), "label": j.get("label") or j.get("kind") or "",
        "status": j.get("status", ""),
        "latest": (j.get("log") or [{}])[-1].get("line", ""),
    } for j in active_jobs]
    # Helper output built here, in Python, is already escaped once. Mark it so
    # the template does not escape it again.
    sweeps_vm = [{
        "batch_id": b.get("batch_id"), "label": Markup(scan_label(b)),
        "total": b.get("total", 0), "done": b.get("done", 0),
        "bar": Markup(progress_bar(b.get("done", 0), b.get("total", 0))),
        "latest": b.get("latest") or "",
    } for b in recent[:6]]
    return _render("overview.html", title="Overview", active="overview", csrf=csrf,
                   badges={"jobs": len(active_jobs)} if active_jobs else {},
                   markets=list(markets), kpis=kpis, jobs=jobs_vm, sweeps=sweeps_vm)


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
        f'<td class="muted">{esc((j.get("created_at").strftime("%b %d %H:%M")) if j.get("created_at") else "")}</td>'
        "</tr>"
        for j in jobs_list
    )
    empty = ('<tr><td colspan=3 class=muted>Nothing has run yet. Every scan and '
            'every "write talking points" job shows up here, and keeps going '
            'even if you close this tab.</td></tr>')
    body = ('<div class="topbar"><h1>Activity</h1></div>'
            '<p class="lede">Everything the engine has run, newest first.</p>'
            "<table><tr><th>What ran</th><th>Status</th><th>Started</th></tr>"
            f"{rows or empty}</table>")
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
  function sortBy(th) {
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
    table.querySelectorAll('th[data-sort]').forEach(function (h) {
      h.setAttribute('aria-sort', h === th ? (sortState.dir === 1 ? 'ascending' : 'descending') : 'none');
    });
    rows.sort(function (a, b) {
      var av = a.dataset['sort_' + key], bv = b.dataset['sort_' + key];
      var an = parseFloat(av), bn = parseFloat(bv);
      var cmp = (!isNaN(an) && !isNaN(bn)) ? (an - bn) : String(av).localeCompare(String(bv));
      return cmp * sortState.dir;
    });
    rows.forEach(function (row) { tbody.appendChild(row); });
  }
  table.querySelectorAll('th[data-sort]').forEach(function (th) {
    // Keyboard operable: a header a mouse can click but a keyboard cannot
    // reach is not operable at all for someone who does not use a mouse.
    th.setAttribute('tabindex', '0');
    th.setAttribute('role', 'button');
    th.setAttribute('aria-sort', 'none');
    th.addEventListener('click', function () { sortBy(th); });
    th.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortBy(th); }
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
            action = f'<a href="/{esc(r["report_slug"])}">report</a>'
        elif state == "approved":
            action = (f'<form class="inline" method="post" action="/console/audits/'
                      f'{esc(r["audit_id"])}/publish">{csrf_field(csrf)}'
                      f'<button type="submit">Publish</button></form>')
        elif state == "draft":
            action = f'<a href="/console/audits/{esc(r["audit_id"])}">review draft</a>'
        else:
            action = f'<a href="/console/audits/{esc(r["audit_id"])}">open</a>'

        contact_html = contact_cell(r.get("contacts") or [])
        # A touch can only be logged once a report exists to have sent.
        outreach_html = outreach_cell(
            r.get("sequence"),
            prospect_id=r.get("prospect_id") or "",
            audit_id=r.get("audit_id"),
            csrf=csrf,
            can_start=bool(r.get("report_slug")),
        )
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
            f'<td>{contact_html}</td>'
            f'<td>{outreach_html}</td>'
            f"<td>{' '.join(tags)}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )

    live = ""
    if progress and progress.get("total"):
        done, total = progress.get("done", 0), progress["total"]
        state = "Done." if done >= total else f"{done} of {total} websites checked."
        live = (f'<div class="banner">{state} <div style="margin-top:6px">'
                f'{progress_bar(done, total)}</div></div>')

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
<th data-sort="total">Total<span class="sub">out of 100</span></th><th>Phone</th>
<th>Contact<span class="sub">who to write to</span></th>
<th>Outreach<span class="sub">four touches, then stop</span></th><th></th><th>Next step</th></tr></thead>
<tbody>
{"".join(table_rows) or '<tr><td colspan="12" class="muted">No websites checked yet. This finishes on its own; check back in a few minutes or watch <a href="/console/jobs">Activity</a>.</td></tr>'}
</tbody></table>
{_BATCH_FILTER_SCRIPT}
"""
    return shell(f"Call list {batch_id}", body, active="batches")


def render_batches(batches: Sequence[Mapping[str, Any]], *, days: int = 14) -> str:
    windows = "".join(
        f'<a class="{"on" if d == days else ""}" href="/console/batches?days={d}">{label}</a>'
        for d, label in ((14, "2 weeks"), (90, "3 months"), (365, "a year"), (3650, "everything"))
    )
    rows = "".join(
        f'<tr><td><a href="/console/batches/{esc(b["batch_id"])}">{scan_label(b)}</a></td>'
        f'<td class="num">{b.get("total", 0)}</td><td class="num">{b.get("done", 0)}</td>'
        f'<td class="num">{b.get("running", 0)}</td><td class="num">{b.get("pending", 0)}</td>'
        f'<td>{progress_bar(b.get("done", 0), b.get("total", 0))}</td>'
        f'<td class="muted">{esc(b.get("latest") or "")}</td></tr>'
        for b in batches
    )
    empty = ('<tr><td colspan=7 class=muted>No scans in this stretch. '
             '<a href="/console">Start one</a>, it takes a couple of minutes for a '
             'small city. If you are looking for an older scan, widen the range '
             'above.</td></tr>')
    body = ('<div class="topbar"><h1>Results</h1></div>'
            '<p class="lede">Open a scan to see who to call.</p>'
            f'<div class="windows">Showing {windows}</div>'
            "<table><tr><th>Scan</th><th>Companies</th><th>Checked</th>"
            "<th>Running</th><th>Waiting</th><th>Progress</th><th>Last activity</th></tr>"
            + (rows or empty)
            + "</table>")
    return shell("Results", body, active="batches")


# ── Audit detail, where approval happens ──────────────────────────────────────

_STATUS_CLASS = {"pass": "pass", "fail": "fail", "skipped": "skip", "error": "fail"}


def findings_predate_audit(findings: Mapping[str, Any] | None,
                           audit: Mapping[str, Any]) -> bool:
    """Whether this site was checked again after its findings were written.

    An audit document is keyed by prospect and batch, so re-checking a site
    overwrites its results in place while the findings keep the text drafted
    against the older ones. A contractor who fixed the very thing we named
    would still read it named, which is the one mistake this report cannot
    afford. Comparing the two timestamps is enough to say so out loud.
    """
    drafted_at = (findings or {}).get("drafted_at")
    started_at = audit.get("started_at")
    if drafted_at is None or started_at is None:
        return False
    try:
        return started_at > drafted_at
    except TypeError:
        # Mixed tz-aware and naive timestamps: not worth a false warning.
        return False


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

    landing = audit.get("landing_url")
    landing_note = ""
    if landing:
        from urllib.parse import urlparse

        path = urlparse(landing).path or "/"
        if path not in ("", "/"):
            landing_note = (
                f'<div class="banner">Scored against <a href="{esc(landing)}" '
                f'target="_blank" rel="noopener noreferrer">{esc(path)}</a>, not the '
                "front page. That is where their Google listing sends people, so it "
                "is what a homeowner actually sees first.</div>")

    findings_block = ""
    if findings:
        state = findings.get("status")
        pool = list(findings.get("findings") or [])
        selected = [int(o) for o in (findings.get("selected") or [])]
        draft = state == "draft"

        def card(f: Mapping[str, Any], position: int) -> str:
            ordinal = int(f.get("ordinal") or 0)
            flags = (f'<p class="muted">flagged: {esc(", ".join(f.get("mechanism_flags") or []))}</p>'
                     if f.get("mechanism_flags") else "")
            if draft:
                # The top three are pre-ticked because that is the model's
                # ranking, not because it is the answer. Rule 7 is the person
                # changing it.
                checked = " checked" if position <= 3 else ""
                head = (f'<label class="pick"><input type="checkbox" name="selected" '
                        f'value="{ordinal}"{checked}> '
                        f'<strong>{esc(f.get("what_we_saw"))}</strong></label>')
            else:
                if ordinal in selected:
                    tag = f'<span class="tag ok">report, number {selected.index(ordinal) + 1}</span>'
                else:
                    later = [o for o in
                             [int(x.get("ordinal") or 0) for x in pool]
                             if o not in selected]
                    tag = (f'<span class="tag dim">follow up {later.index(ordinal) + 1}</span>'
                           if ordinal in later else "")
                head = f'<h3>{tag} {esc(f.get("what_we_saw"))}</h3>'
            return (f'<div class="finding">{head}'
                    f'<p>{esc(f.get("what_it_means"))}</p>'
                    f'<p><strong>{esc(f.get("what_fixing_takes"))}</strong></p>{flags}</div>')

        cards = "".join(card(f, i + 1) for i, f in enumerate(pool))

        warn = ""
        if findings_predate_audit(findings, audit):
            warn += ('<div class="banner">This site was checked again after these '
                     'were written, so they describe what we saw last time. Anything '
                     'he has fixed since would still be named here. Write them again '
                     'before sending this to him.</div>')
        if findings.get("needs_review"):
            warn = ('<div class="banner">Read this before approving. Some wording may '
                    'describe how we found the problem rather than what the owner would '
                    'notice. He should hear what a customer experiences, never how we '
                    'measured it.</div>')

        if draft:
            action = ('<p class="hint">Tick the three he should read. They go in the '
                      'report in the order they appear here. Whatever you leave '
                      'unticked is held back, one per follow up, so each message '
                      'after the first has something new in it. Choosing does not '
                      'send or publish anything.</p>'
                      f'<button type="submit">Use these three</button>')
            cards = (f'<form method="post" action="/console/audits/{esc(audit_id)}/approve">'
                     f'{csrf_field(csrf)}{cards}{action}</form>')
            action = ""
        elif state == "approved" and not audit.get("report_slug"):
            action = (f'<form method="post" action="/console/audits/{esc(audit_id)}/publish">'
                      f'{csrf_field(csrf)}<button type="submit">Create the shareable report</button></form>')
        elif audit.get("report_slug"):
            action = (f'<p><a href="/{esc(audit["report_slug"])}" target="_blank" '
                      f'rel="noopener noreferrer">Open the report you can share</a></p>')
        else:
            action = ""

        held = max(0, len(pool) - 3)
        note = ""
        if state != "draft" and held < 3:
            note = (f'<p class="muted">{held} held back, so this company gets '
                    f'{held + 1} message{"s" if held else ""} rather than four. '
                    'There was not enough wrong with the site to say something new '
                    'a fourth time.</p>')
        findings_block = (f"<h2>Talking points <span class='tag'>{esc(state)}</span></h2>"
                          f"{warn}{cards}{action}{note}")
    else:
        findings_block = (f"""<h2>Talking points</h2><div class="card">
<p class="muted">Nothing written yet. We will rank the problems costing this company
the most work and explain each in plain language. You pick the three he reads.</p>
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
{landing_note}
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
