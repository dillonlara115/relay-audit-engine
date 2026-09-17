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

# Text colour and tint for each segment chip, checked by the palette tests at
# 4.5:1. The swatch dot keeps SEGMENT_COLORS; the words sit on the tint.
SEGMENT_TEXT = {
    "Leaky Bucket": ("#B0400E", "#FDEBE4"),
    "Invisible Pro": ("#1A56C4", "#E6EEFD"),
    "Both Broken": ("#5B3F92", "#EFEAF7"),
    "Dialed": ("#25683F", "#E3F1E8"),
    "incomplete": ("#5d564d", "#EEEBE6"),
}

# Every text-on-surface pair the console uses. The tests hold each to AA.
PALETTE = {
    "asphalt": "#16120E", "ember": "#B0400E", "ink2": "#5d564d",
    "field": "#F7F5F2", "panel": "#ffffff", "chalk": "#ECE6DC",
    "orange": "#F25C1F", "line": "#E6E2DC",
}
PILL_COLORS = {
    "ok": ("#1E5F3A", "#E3F1E8"),
    "warn": ("#7A4A00", "#FBEBD0"),
    "bad": ("#8d2f16", "#F9E3DC"),
    "dim": ("#5d564d", "#EEEBE6"),
    "tint": ("#B0400E", "#FDEBE4"),
    "info": ("#1A56C4", "#E6EEFD"),
    "running": ("#16120E", "#F25C1F"),
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
                     ("/console/batches", "batches", "Sweeps", "list"),
                     ("/console/templates", "templates", "Email templates", "mail")]),
    ("System", [("/console/jobs", "jobs", "Jobs", "activity")]),
]

# Older call sites still say active="console" or "dashboard".
_ACTIVE_ALIASES = {"console": "overview", "dashboard": "overview"}


def _markup(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Markup:
        return Markup(fn(*args, **kwargs))
    return wrapped


# A notice is a headline chosen by code plus a detail chosen by the caller.
# Both ride the query string on a redirect, because Firebase Hosting forwards
# only the session cookie and a cookie flash would never arrive. The headline
# is fixed per code so a crafted link cannot invent one; the detail is capped
# and escaped like any other string.
NOTICES = {
    "not_approved": "Findings not approved.",
    "publish_blocked": "Report not published.",
    "not_recorded": "Nothing was recorded.",
    "reaudit_queued": "Re-audit queued.",
    "templates_saved": "Templates saved.",
    "templates_rejected": "Templates not saved.",
}
NOTICE_DETAIL_CAP = 200


def notice_from(code: str | None, detail: str | None) -> tuple[str, str] | None:
    headline = NOTICES.get(code or "")
    if not headline:
        return None
    return headline, (detail or "")[:NOTICE_DETAIL_CAP]


def icon(name: str) -> Markup:
    return Markup(f'<svg class="ic" aria-hidden="true"><use href="#i-{esc(name)}"></use></svg>')



def _tag_titles():
    from app.console import calllist
    return calllist.TAG_TITLES

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
    # Registered as environment globals, not just context: a macro imported
    # from another file does not see the caller's context, and the Outreach
    # card is one. Idempotent, so every render can afford to call it.
    _env.globals.setdefault("icon", icon)
    _env.globals.setdefault("tag_titles", _tag_titles())
    for name in ("csrf_field", "status_pill", "chip", "tiles", "progress_bar",
                 "scan_label", "score_headers", "score_legend", "contact_cell",
                 "outreach_cell"):
        _env.globals.setdefault(name, _markup(globals()[name]))
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


def shell(title: str, body: str, *, active: str = "console", script: str = "",
          notice: tuple[str, str] | None = None) -> str:
    """Wrapper for screens not yet on their own template.

    The body is trusted HTML the caller built, wrapped the way the old shell
    did it. Each screen leaves this behind as it gets a template.
    """
    return _render("base.html", title=title, body=Markup(_wrap_tables(body)),
                   active=active, script=Markup(script), notice=notice)


def csrf_field(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(token)}">'


def chip(segment: str | None) -> str:
    name = segment or "incomplete"
    color = SEGMENT_COLORS.get(name, SEGMENT_COLORS["incomplete"])
    text, tint = SEGMENT_TEXT.get(name, SEGMENT_TEXT["incomplete"])
    label = "Incomplete" if name == "incomplete" else name
    return (f'<span class="chip" style="color:{text};background:{tint}">'
            f'<i style="background:{color}"></i>{esc(label)}</span>')


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


def scan_title(batch: Mapping[str, Any]) -> str:
    """scan_label without the markup: for a breadcrumb, a <title>, a table
    cell the template escapes. 'Fort Collins, Sep 16', or the market, or the id."""
    market = batch.get("market")
    started = batch.get("started_at")
    if market and started:
        return f"{market}, {started.strftime('%b %d')}"
    return str(market or batch.get("batch_id", ""))


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

SCORE_SUBS = {"F": "found", "C": "chosen", "B": "booked"}

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
  <summary>What the scores mean</summary>
  <div class="inner">
    <p>Think of a homeowner whose roof is leaking. They go through three steps,
    and each prospect is scored out of 100 on how well it handles them.</p>
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
    <h4>Segments</h4>
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
    <h4>Tags</h4>
    <p><span class="tag warn">Partial</span> {esc(_tag_titles()["Partial"])}</p>
    <p><span class="tag">Agency</span> {esc(_tag_titles()["Agency"])}</p>
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


def render_templates(templates: Mapping[str, Any] | None, *, csrf: str,
                     notice: tuple[str, str] | None = None) -> str:
    """The four emails as the operator has written them, with the variables
    that can go in. Saving validates; nothing here sends or renders a real
    prospect, that happens on the prospect page."""
    from app import outreach_templates as tpl

    rows = []
    for n, row in enumerate(tpl.sequence_of(templates), start=1):
        rows.append({"n": n, "subject": row["subject"], "body": row["body"],
                     "when": {1: "Day 0, with the report link and its three findings",
                              2: "Day 3, one held-back finding",
                              3: "Day 7, one held-back finding",
                              4: "Day 14, one held-back finding, then the sequence closes"}[n]})
    return _render("templates.html", title="Email templates", active="templates", csrf=csrf,
                   notice=notice, rows=rows, variables=list(tpl.VARIABLES.items()),
                   saved=bool(templates))


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
    """Where this prospect sits in the four-email sequence, as a pill.

    A pill and a link, never a form. The call list is for reading state; the
    prospect page is where Compose email and Mark as sent live, with the
    explanation around them. The old button here read as a send action and
    was clicked as one. `csrf` stays in the signature for the callers that
    pass it and is not used.
    """
    from app import outreach

    href = f"/console/audits/{esc(audit_id or '')}#outreach"

    def pill(kind: str, text: str) -> str:
        return f'<a class="pill {kind}" href="{href}">{esc(text)}</a>'

    if not sequence:
        return pill("tint", "Due: first email") if can_start else pill("dim", "Not started")
    seq = outreach.Sequence.from_dict(sequence)
    if seq.status == outreach.CLOSED:
        reason = outreach.INTENT_LABELS.get(seq.last_intent or "") or seq.closed_reason or "finished"
        return pill("dim", f"Closed: {reason}")
    if seq.status == outreach.WAITING:
        return pill("warn", f"Waiting: {outreach.park_reason(seq)}")
    if seq.touch_count == 0:
        return pill("tint", "Due: first email") if can_start else pill("dim", "Not started")
    label = f"{seq.touch_count} of {seq.max_touches} sent"
    if seq.due():
        return pill("warn", f"Due today, {label}")
    if seq.next_due_at:
        label += f", next {seq.next_due_at.strftime('%b %d')}"
    return pill("tint", label)


def render_run(*, csrf: str, markets: Sequence[str], active_jobs: Sequence[Mapping[str, Any]],
               recent_batches: Sequence[Mapping[str, Any]],
               notice: tuple[str, str] | None = None) -> str:
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
        "latest_iso": (b.get("latest_at").isoformat() if hasattr(b.get("latest_at"), "isoformat") else ""),
        "pct": round(100 * int(b.get("done") or 0) / int(b.get("total") or 1)),
    } for b in recent[:6]]
    return _render("overview.html", title="Overview", active="overview", csrf=csrf,
                   badges={"jobs": len(active_jobs)} if active_jobs else {},
                   markets=list(markets), kpis=kpis, jobs=jobs_vm, sweeps=sweeps_vm,
                   notice=notice)


def render_job(job: Mapping[str, Any], *, csrf: str,
               notice: tuple[str, str] | None = None) -> str:
    """One job: live status, the streamed log, and the one next step."""
    status = job.get("status", "queued")
    result = job.get("result") or {}
    job_id = str(job.get("job_id") or "")
    followup = None
    if status == "done" and result.get("batch_id"):
        followup = "sweep" if job.get("kind") == "sweep" else "batch"
    vm = {
        "job_id": job_id,
        "label": job.get("label") or job.get("kind") or "",
        "status": status,
        "lines": "\n".join(entry.get("line", "") for entry in (job.get("log") or [])),
        "error": job.get("error") or "",
        "answer": result.get("answer") or "",
        "followup": followup,
        "eligible": result.get("eligible", 0),
        "batch_id": result.get("batch_id") or "",
        "market": (job.get("params") or {}).get("market") or "",
    }
    live = status in ("queued", "running")
    script = _env.get_template("_scripts.html").module.poll() if live else Markup("")
    return _render("job.html", title=f"Job {job_id}", active="jobs", csrf=csrf, job=vm,
                   body_attrs=Markup(f'data-job="{esc(job_id)}"'), script=script,
                   notice=notice)


_KIND_LABEL = {"sweep": "Sweep", "audit": "Audit", "draft": "Draft",
               "agent": "Coordinator", "dispatch": "Dispatch"}


def render_jobs(jobs_list: Sequence[Mapping[str, Any]], *,
                notice: tuple[str, str] | None = None) -> str:
    rows = [{
        "job_id": j.get("job_id"),
        "label": j.get("label") or j.get("kind") or "",
        "kind": _KIND_LABEL.get(str(j.get("kind") or ""), str(j.get("kind") or "").title()),
        "status": j.get("status", ""),
        "started": j["created_at"].strftime("%b %d %H:%M") if j.get("created_at") else "",
        "started_iso": (j.get("created_at").isoformat() if hasattr(j.get("created_at"), "isoformat") else ""),
    } for j in jobs_list]
    return _render("jobs.html", title="Jobs", active="jobs", rows=rows, notice=notice)


# ── Batch screen, with actions ────────────────────────────────────────────────


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


def _reaudit_confirm(business_name: str) -> Markup:
    """An onsubmit attribute whose string survives any name. json.dumps makes
    the JS literal; esc makes the attribute. The browser undoes the second
    before the first runs."""
    text = f"Re-audit {business_name}? This queues a fresh audit and overwrites the scores on this call list."
    return Markup(esc("return confirm(" + json.dumps(text) + ")"))


def render_batch(batch_id: str, rows: Sequence[Mapping[str, Any]],
                 segments: Mapping[str, int], check_defs: Sequence[Mapping[str, Any]] = (),
                 *, csrf: str, progress: Mapping[str, Any] | None = None,
                 notice: tuple[str, str] | None = None, tab: str = "all",
                 counts: Mapping[str, int] | None = None,
                 sweep_label: str | None = None,
                 excluded: Sequence[Mapping[str, Any]] = (),
                 excluded_known: bool = True) -> str:
    """The call list: one sweep's prospects in call order, under tabs."""
    from app.console import calllist

    tab = calllist.normalize_tab(tab)
    counts = dict(counts) if counts is not None else calllist.tab_counts(segments)
    if tab == "excluded" and "excluded" not in counts:
        counts["excluded"] = len(excluded)
    shown = calllist.filter_rows(rows, tab=tab) if tab != "excluded" else []

    vm = []
    for r in shown:
        scores = r.get("scores") or {}
        needle = f'{r.get("business_name") or ""} {r.get("city") or ""}'.lower()
        # These attributes are what the filter script and the sort read, and a
        # test pins their exact escaping. Built here with esc() and marked, so
        # they come out byte for byte as they always did.
        attrs = (
            f'data-business="{esc(needle)}" data-segment="{esc(r.get("segment") or "incomplete")}" '
            f'data-checks="{esc(json.dumps(r.get("checks") or {}, separators=(",", ":")))}" '
            f'data-sort_rank="{esc(r.get("rank"))}" '
            f'data-sort_business="{esc((r.get("business_name") or "").lower())}" '
            f'data-sort_found="{scores.get("found", -1)}" '
            f'data-sort_chosen="{scores.get("chosen", -1)}" '
            f'data-sort_booked="{scores.get("booked", -1)}" '
            f'data-sort_total="{scores.get("total", -1)}"'
        )
        vm.append({
            "attrs": Markup(attrs),
            "rank": r.get("rank"), "audit_id": r.get("audit_id") or "",
            "business_name": r.get("business_name") or "", "city": r.get("city") or "",
            "chip": Markup(chip(r.get("segment"))),
            "found": scores.get("found", ""), "chosen": scores.get("chosen", ""),
            "booked": scores.get("booked", ""), "total": scores.get("total", ""),
            "phone": r.get("phone") or "",
            "contact": Markup(contact_cell(r.get("contacts") or [])),
            "outreach": Markup(outreach_cell(
                r.get("sequence"), prospect_id=r.get("prospect_id") or "",
                audit_id=r.get("audit_id"), csrf=csrf, can_start=bool(r.get("report_slug")))),
            "findings": calllist.findings_state(r),
            "tags": calllist.row_tags(r),
            "report_slug": r.get("report_slug") or "",
            "can_publish": (not r.get("report_slug")) and r.get("findings_status") == "approved",
            "reaudit_confirm": _reaudit_confirm(str(r.get("business_name") or "this prospect")),
        })

    live = None
    if progress and progress.get("total"):
        done, total = int(progress.get("done", 0) or 0), int(progress["total"])
        live = {"text": "All audits finished." if done >= total else f"{done} of {total} audits finished.",
                "bar": Markup(progress_bar(done, total))}

    excluded_vm = calllist.excluded_rows_vm(excluded) if tab == "excluded" else []
    for e in excluded_vm:
        e["attrs"] = Markup(f'data-business="{esc(e["needle"])}"')

    return _render("calllist.html", title=f"Call list: {sweep_label or batch_id}", active="batches", csrf=csrf,
                   batch_id=batch_id, rows=vm, tab=tab,
                   tabs=calllist.visible_tabs(counts), progress=live,
                   check_options=Markup(_check_filter_options(check_defs)),
                   sweep_label=sweep_label or batch_id, notice=notice,
                   excluded=excluded_vm, excluded_known=excluded_known,
                   export_href=f"/console/batches/{esc(batch_id)}/export.csv?tab={tab}")


def _sweep_state(b: Mapping[str, Any]) -> tuple[str, str]:
    """A sweep's pill, from the ledger counts. Finished when every audit is
    done; Failed when the rest failed; Running while anything is still owed."""
    total = int(b.get("total") or 0)
    done = int(b.get("done") or 0)
    failed = int(b.get("failed") or 0)
    if not total:
        return "dim", "Queued"
    if done >= total:
        return "ok", "Finished"
    if failed and done + failed >= total:
        return "bad", "Failed"
    return "tint", "Running"


SWEEP_WINDOWS = ((14, "2 weeks"), (90, "3 months"), (365, "a year"), (3650, "everything"))


def render_batches(batches: Sequence[Mapping[str, Any]], *, days: int = 14,
                   notice: tuple[str, str] | None = None) -> str:
    rows = [{
        "batch_id": b.get("batch_id"),
        "label": Markup(scan_label(b)),
        "state": _sweep_state(b),
        "total": b.get("total", 0), "done": b.get("done", 0),
        "running": b.get("running", 0), "pending": b.get("pending", 0),
        "failed": b.get("failed", 0),
        "bar": Markup(progress_bar(b.get("done", 0), b.get("total", 0))),
        "latest": b.get("latest") or "",
        "latest_iso": (b.get("latest_at").isoformat() if hasattr(b.get("latest_at"), "isoformat") else ""),
        "pct": round(100 * int(b.get("done") or 0) / int(b.get("total") or 1)),
        "title": scan_title(b),
    } for b in batches]
    return _render("sweeps.html", title="Sweeps", active="batches", rows=rows,
                   windows=SWEEP_WINDOWS, days=days, notice=notice)


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


def _confirm_attr(text: str) -> Markup:
    """An onsubmit attribute whose string survives any name. json.dumps makes
    the JS literal; esc makes the attribute. The browser undoes the second
    before the first runs."""
    return Markup(esc("return confirm(" + json.dumps(text) + ")"))


def outreach_state(sequence: Mapping[str, Any] | None, *, can_start: bool) -> tuple[str, str]:
    """One pill for where a prospect sits in the four-email sequence."""
    from app import outreach

    if not sequence:
        return ("tint", "Due: first email") if can_start else ("dim", "Not started")
    seq = outreach.Sequence.from_dict(sequence)
    if seq.status == outreach.CLOSED:
        reason = outreach.INTENT_LABELS.get(seq.last_intent or "") or seq.closed_reason or "finished"
        return "dim", f"Closed: {reason}"
    if seq.status == outreach.WAITING:
        return "warn", f"Waiting: {outreach.park_reason(seq)}"
    if seq.touch_count == 0:
        return ("tint", "Due: first email") if can_start else ("dim", "Not started")
    label = f"{seq.touch_count} of {seq.max_touches} sent"
    if seq.due():
        return "warn", f"Due today, {label}"
    if seq.next_due_at:
        label += f", next {seq.next_due_at.strftime('%b %d')}"
    return "tint", label


def outreach_context(*, audit: Mapping[str, Any], prospect: Mapping[str, Any],
                     findings: Mapping[str, Any] | None,
                     sequence: Mapping[str, Any] | None,
                     touches: Sequence[Mapping[str, Any]],
                     replies: Sequence[Mapping[str, Any]],
                     report_url: str | None, signature: str, sender_name: str = "",
                     templates: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Everything the Outreach card shows, computed once and testable.

    Compose builds a mailto: link and nothing else; Mark as sent posts to the
    ledger route that records a send a person already made. Neither transmits
    a byte, and the card says so in a fixed sentence.
    """
    from app import outreach
    from app.console import compose as composer
    from app.report.publish import followup_findings

    published = bool(audit.get("report_slug"))
    seq = outreach.Sequence.from_dict(sequence) if sequence else None
    pool = list((findings or {}).get("findings") or [])
    later = followup_findings(findings) if findings else []
    max_touches = seq.max_touches if seq else (outreach.touches_supported(len(pool)) or 1)
    sent = seq.touch_count if seq else 0
    is_open = seq.is_open if seq else True
    next_ordinal = sent + 1
    owner_email = prospect.get("owner_email") or None
    recipient = owner_email or "the owner (no address on record)"

    step1: dict[str, Any] = {"mode": "hidden"}
    if is_open and next_ordinal <= max_touches:
        if not published:
            step1 = {"mode": "unpublished"}
        else:
            draft = composer.compose(ordinal=next_ordinal, prospect=prospect,
                                     report_url=report_url or "", findings_doc=findings,
                                     signature=signature, sender_name=sender_name,
                                     templates=templates)
            if next_ordinal == 1:
                note = (f"Opens your mail client with the report link and the three "
                        f"findings for {owner_email}." if owner_email else "")
            else:
                fu = later[next_ordinal - 2] if len(later) >= next_ordinal - 1 else {}
                seen = " ".join(str(fu.get("what_we_saw") or "").split())
                note = (f"Opens your mail client with follow-up finding {next_ordinal - 1}: "
                        f"{seen[:80]}{'...' if len(seen) > 80 else ''}")
            step1 = {"mode": "compose" if owner_email else "no_address",
                     "href": composer.fit_mailto(draft), "note": note,
                     "subject": draft.subject, "body": draft.body,
                     "warnings": list(draft.warnings)}

    step2 = {"show": published and is_open and next_ordinal <= max_touches,
             "confirm": _confirm_attr(
                 f"Mark email {next_ordinal} of {max_touches} to {recipient} as sent? "
                 "This only records that you already sent it from your own mailbox. "
                 "Nothing is sent from here.")}

    events: list[tuple[Any, str, str]] = []
    for t in touches:
        when = t.get("sent_at")
        stamp = when.strftime("%b %d") if hasattr(when, "strftime") else ""
        events.append((when, "sent", f"Email {t.get('ordinal', '?')} sent {stamp}".strip()))
    for r in replies:
        when = r.get("received_at")
        stamp = when.strftime("%b %d") if hasattr(when, "strftime") else ""
        label = outreach.INTENT_LABELS.get(str(r.get("intent") or ""), str(r.get("intent") or "reply"))
        excerpt = " ".join(str(r.get("excerpt") or "").split())[:120]
        who = f" ({r.get('from_email')})" if r.get("from_email") else ""
        text = f"Reply {stamp}: {label}{who}".strip()
        if excerpt:
            text += f'. "{excerpt}"'
        events.append((when, "reply", text))
    events.sort(key=lambda e: (e[0] is None, e[0] or 0))
    timeline = [{"kind": k, "text": t} for _, k, t in events]

    nxt = None
    if seq and seq.status == outreach.CLOSED:
        _, text = outreach_state(sequence, can_start=published)
        nxt = text + "."
    elif seq and seq.status == outreach.WAITING:
        nxt = (f"Waiting: {outreach.park_reason(seq)}. Log the reply outcome from the CLI "
               "(python -m app.cli replies) to continue.")
    elif sent and seq and seq.next_due_at:
        due = seq.next_due_at.strftime("%b %d")
        fu = later[next_ordinal - 2] if next_ordinal >= 2 and len(later) >= next_ordinal - 1 else None
        if fu:
            seen = " ".join(str(fu.get("what_we_saw") or "").split())
            nxt = f"Next due {due}, carrying follow-up finding {next_ordinal - 1}: {seen[:120]}"
        else:
            nxt = f"Next due {due}. No findings left for another email; the sequence closes after this one."

    return {"state": outreach_state(sequence, can_start=published),
            "step1": step1, "step2": step2, "timeline": timeline, "next": nxt}


def render_audit(*, audit: Mapping[str, Any], prospect: Mapping[str, Any],
                 checks: Sequence[Mapping[str, Any]], definitions: Mapping[str, Any],
                 findings: Mapping[str, Any] | None, evidence: Sequence[Mapping[str, Any]],
                 csrf: str, notice: tuple[str, str] | None = None,
                 sequence: Mapping[str, Any] | None = None,
                 touches: Sequence[Mapping[str, Any]] = (),
                 replies: Sequence[Mapping[str, Any]] = (),
                 history: Sequence[Mapping[str, Any]] | None = (),
                 report_url: str | None = None,
                 signature: str = "Relay for Roofers", sender_name: str = "",
                 templates: Mapping[str, Any] | None = None,
                 sweep_label: str | None = None) -> str:
    """One prospect: scores, findings, outreach, every check, the evidence."""
    from urllib.parse import urlparse

    from app.console import calllist

    scores = audit.get("scores") or {}
    audit_id = str(audit.get("audit_id") or audit.get("id") or "")
    prospect_id = str(audit.get("prospect_id") or prospect.get("place_id") or "")
    name = str(prospect.get("business_name") or "Prospect")

    # ── header ────────────────────────────────────────────────────────────
    website = prospect.get("website_url") or "#"
    lede = (f'{esc(prospect.get("city") or "")} &middot; {esc(prospect.get("gbp_phone") or "")} &middot; '
            f'<a href="{esc(website)}" target="_blank" rel="noopener noreferrer">'
            f'{esc(prospect.get("domain") or "no website")}</a>')
    if prospect.get("maps_uri"):
        lede += (f' &middot; <a href="{esc(prospect["maps_uri"])}" target="_blank" '
                 'rel="noopener noreferrer">Google Business Profile</a>')
    state = (findings or {}).get("status")
    if audit.get("report_slug"):
        primary = Markup(f'<a class="btn" href="/{esc(audit["report_slug"])}" target="_blank" '
                         'rel="noopener noreferrer">Open report</a>')
    elif state == "approved":
        primary = Markup(f'<form method="post" action="/console/audits/{esc(audit_id)}/publish">'
                         f'{csrf_field(csrf)}<button type="submit">Publish report</button></form>')
    elif not findings:
        primary = Markup(f'<form method="post" action="/console/audits/{esc(audit_id)}/draft">'
                         f'{csrf_field(csrf)}<button type="submit">Draft findings</button></form>')
    else:
        primary = None

    landing = None
    if audit.get("landing_url"):
        path = urlparse(audit["landing_url"]).path or "/"
        if path not in ("", "/"):
            landing = {"url": audit["landing_url"], "path": path}
    partial = list(audit.get("partial_sections") or [])
    finished = audit.get("finished_at")

    p_vm = {
        "audit_id": audit_id, "prospect_id": prospect_id, "name": name,
        "batch_id": audit.get("batch_id") or "", "sweep_label": sweep_label or "Call list",
        "lede": Markup(lede), "primary": primary,
        "scores": {k: scores.get(k, 0) for k in ("found", "chosen", "booked", "total")},
        "chip": Markup(chip(audit.get("segment"))), "band": audit.get("band") or "",
        "landing": landing, "crawl_error": audit.get("crawl_error") or "",
        "partial_sections": ", ".join(partial), "partial_count": len(partial),
        "audited": finished.strftime("%b %d, %Y") if hasattr(finished, "strftime") else "",
        "report_slug": audit.get("report_slug") or "",
        "findings_state": calllist.findings_state({"report_slug": audit.get("report_slug"),
                                                   "findings_status": state}),
        "reaudit_confirm": _confirm_attr(f"Re-audit {name}? This queues a fresh audit."),
        "suppress_confirm": _confirm_attr(f"Never contact {name} again? This cannot be undone here."),
    }

    # ── findings ──────────────────────────────────────────────────────────
    f_vm = None
    if findings:
        pool = list(findings.get("findings") or [])
        selected = [int(o) for o in (findings.get("selected") or [])]
        draft = state == "draft"
        later = [int(x.get("ordinal") or 0) for x in pool
                 if int(x.get("ordinal") or 0) not in selected]
        cards = []
        for position, item in enumerate(pool, start=1):
            ordinal = int(item.get("ordinal") or 0)
            tag = None
            if not draft:
                if ordinal in selected:
                    tag = ("ok", f"report, number {selected.index(ordinal) + 1}")
                elif ordinal in later:
                    tag = ("dim", f"follow up {later.index(ordinal) + 1}")
            cards.append({"ordinal": ordinal, "preticked": position <= 3, "tag": tag,
                          "saw": item.get("what_we_saw") or "",
                          "means": item.get("what_it_means") or "",
                          "fix": item.get("what_fixing_takes") or "",
                          "flags": ", ".join(item.get("mechanism_flags") or [])})
        held = max(0, len(pool) - 3)
        thin = ""
        if not draft and held < 3:
            thin = (f"{held} held back, so this prospect gets {held + 1} "
                    f"email{'s' if held else ''} rather than four. There was not enough "
                    "wrong with the site to say something new a fourth time.")
        f_vm = {"draft": draft, "cards": cards, "stale": findings_predate_audit(findings, audit),
                "needs_review": bool(findings.get("needs_review")),
                "can_publish": state == "approved" and not audit.get("report_slug"),
                "thin_note": thin}

    # ── checks by section ─────────────────────────────────────────────────
    subs = {
        "found": "Can a homeowner searching for a roofer find them at all?",
        "chosen": "Once found, do they look like a safe choice?",
        "booked": "If someone wants to hire them, can they actually get through? "
                  "Worth the most, because this is where jobs quietly go missing.",
        "measurement": "Background information only. Not scored.",
    }
    grouped: dict[str, list] = {k: [] for k in subs}
    for c in sorted(checks, key=lambda c: definitions.get(c.get("code"), {}).get("sort_order", 0)):
        d = definitions.get(c.get("code")) or {}
        if d.get("section") in grouped:
            status = str(c.get("status") or "")
            grouped[d["section"]].append({
                "code": c.get("code") or "", "title": d.get("title") or "",
                "cls": _STATUS_CLASS.get(status, ""), "result": status.title(),
                "points": f"{c.get('points_awarded', 0)}/{d.get('points', 0)}",
                "points_awarded": c.get("points_awarded", 0),
                "note": c.get("note") or "",
            })
    sections = [{"title": k.title(), "sub": subs[k], "rows": v} for k, v in grouped.items() if v]

    # ── evidence ──────────────────────────────────────────────────────────
    def evidence_item(e: Mapping[str, Any]) -> str:
        url = e.get("url")
        kb = round((e.get("size_bytes") or 0) / 1024)
        caption = (f'<p class="muted evidence-cap">{esc(e.get("kind"))} '
                   f'&middot; {kb} KB captured during the audit</p>')
        if url and e.get("kind") == "screenshot":
            return (f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                    f'<img class="evidence-shot" src="{esc(url)}" '
                    f'alt="Landing page as captured during the audit"></a>{caption}')
        if url:
            return (f'<p><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                    f'{esc(e.get("kind"))}</a>{caption}')
        problem = e.get("url_error")
        return (f'<p class="muted">{esc(e.get("kind"))}: {esc(e.get("gcs_path"))} '
                f'({kb} KB)' + (f' &middot; could not sign a link: {esc(problem)}'
                                if problem else "") + "</p>")

    evidence_html = Markup("".join(evidence_item(e) for e in evidence))

    history_note = None
    if history is None:
        history_note = "Score history is not available yet."
        history = ()
    elif len(history) <= 1:
        history_note = "No earlier audits for this prospect."
    h_vm = [{
        "date": h["finished_at"].strftime("%b %d, %Y") if hasattr(h.get("finished_at"), "strftime") else "",
        "date_iso": (h.get("finished_at").isoformat() if hasattr(h.get("finished_at"), "isoformat") else ""),
        "sweep": h.get("sweep_label") or h.get("batch_id") or "",
        "found": (h.get("scores") or {}).get("found", ""), "chosen": (h.get("scores") or {}).get("chosen", ""),
        "booked": (h.get("scores") or {}).get("booked", ""), "total": (h.get("scores") or {}).get("total", ""),
        "chip": Markup(chip(h.get("segment"))), "partial": bool(h.get("partial")),
        "current": h.get("audit_id") == audit_id,
    } for h in history]

    o_vm = outreach_context(audit=audit, prospect=prospect, findings=findings,
                            sequence=sequence, touches=touches, replies=replies,
                            report_url=report_url, signature=signature, sender_name=sender_name, templates=templates)

    return _render("prospect.html", title=name, active="batches", csrf=csrf,
                   p=p_vm, f=f_vm, o=o_vm, sections=sections, evidence_html=evidence_html,
                   history=h_vm if len(h_vm) > 1 else [], history_note=history_note,
                   notice=notice)
