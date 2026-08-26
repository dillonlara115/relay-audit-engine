"""Operator CLI.

    python -m app.cli doctor
    python -m app.cli sweep "Colorado Springs"
    python -m app.cli show <place_id>

The sweep is the day-one gate: ingest a metro, gate every prospect, print the
result and the reason for each one.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from app.checks.definitions import CHECK_DEFINITIONS, MEASUREMENT, section_points
from app.config import ConfigError, get_config
from app.gate import GATE_FAIL, GATE_PASS, GATE_REVIEW
from app.markets import known_markets, resolve_market
from app.pipeline import (
    DEFAULT_GATE_CONCURRENCY,
    AuditOutcome,
    GateOutcome,
    SweepResult,
    audit_one,
    run_sweep,
)
from app.leases import (
    DONE,
    FAILED,
    MAX_ATTEMPTS,
    PENDING,
    RUNNING,
    seed_tasks,
    tasks_for_batch,
)
from app.scoring import BOOKED, CHOSEN, FOUND
from app.ranker import by_segment, rank
from app.scoring import compute, outcomes_from
from app.tools.pubsub import publish_batch
from app.tools.crawl import Crawler
from app.store import firestore as store

app = typer.Typer(add_completion=False, help="Relay audit engine operator CLI.")


def _console() -> Console:
    """Rich falls back to 80 columns when piped, which eats the reason column.

    The gate output is meant to be read in a terminal and captured to a file,
    and the reason is the whole point, so a piped run gets a usable width.
    """
    override = os.getenv("RELAY_CLI_WIDTH")
    if override and override.isdigit():
        return Console(width=int(override))
    probe = Console()
    return probe if probe.is_terminal else Console(width=150)


console = _console()

RESULT_STYLE = {GATE_PASS: "bold green", GATE_REVIEW: "yellow", GATE_FAIL: "dim red"}


def _truncate(value: str | None, width: int) -> str:
    text = value or ""
    return text if len(text) <= width else text[: width - 1] + "…"


# ── doctor ────────────────────────────────────────────────────────────────────


@app.command()
def doctor() -> None:
    """Prove the three dependencies answer: Vertex, Firestore, Places."""
    cfg = get_config()
    table = Table(title="Preflight", header_style="bold")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail", overflow="fold")

    def row(name: str, ok: bool | None, detail: str) -> None:
        mark = {True: "[green]ok[/]", False: "[red]FAIL[/]", None: "[yellow]skip[/]"}[ok]
        table.add_row(name, mark, detail)

    try:
        cfg.require("project", "places_api_key")
        row("config", True, f"project={cfg.project} model={cfg.gemini_model}")
    except ConfigError as exc:
        row("config", False, str(exc))
        console.print(table)
        raise typer.Exit(code=1)

    # Vertex. One real call, cheapest possible, so quota is confirmed not assumed.
    try:
        from google import genai

        client = genai.Client(
            vertexai=cfg.use_vertexai, project=cfg.project, location=cfg.model_location
        )
        resp = client.models.generate_content(
            model=cfg.gemini_model, contents="Reply with the single word: ready"
        )
        row("vertex", True, f"{cfg.gemini_model} @ {cfg.model_location} -> {(resp.text or '').strip()!r}")
    except Exception as exc:  # noqa: BLE001 - preflight reports, never raises
        row("vertex", False, f"{type(exc).__name__}: {exc}")

    # Firestore round trip.
    try:
        client = store.get_client()
        ref = client.collection("_preflight").document("doctor")
        ref.set({"at": store.utcnow()})
        ok = ref.get().exists
        ref.delete()
        row("firestore", ok, f"database={cfg.firestore_database} write+read+delete")
    except Exception as exc:  # noqa: BLE001
        row("firestore", False, f"{type(exc).__name__}: {exc}")

    # Places, one result, cached for 30 days after the first run.
    try:
        from app.tools.places import search_market

        found = asyncio.run(
            search_market("Colorado Springs, CO", queries=["roofing contractor in {market}"], limit=1)
        )
        detail = found[0].business_name if found else "no results"
        row("places", bool(found), detail)
    except Exception as exc:  # noqa: BLE001
        row("places", False, f"{type(exc).__name__}: {exc}")

    console.print(table)


# ── sweep ─────────────────────────────────────────────────────────────────────


def _results_table(outcomes: list[GateOutcome]) -> Table:
    table = Table(title="Gate results", header_style="bold", show_lines=False)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Business", width=30)
    table.add_column("City", width=14)
    table.add_column("Rev", justify="right", width=4)
    table.add_column("Rating", justify="right", width=6)
    table.add_column("Site", width=3, justify="center")
    table.add_column("Gate", width=6)
    table.add_column("Reason", overflow="fold")

    order = {GATE_PASS: 0, GATE_REVIEW: 1, GATE_FAIL: 2}
    ranked = sorted(
        outcomes,
        key=lambda o: (order.get(o.result, 3), -(o.record.review_count or 0)),
    )

    for index, outcome in enumerate(ranked, start=1):
        record = outcome.record
        style = RESULT_STYLE.get(outcome.result, "")
        table.add_row(
            str(index),
            _truncate(record.business_name, 30),
            _truncate(record.city, 14),
            str(record.review_count) if record.review_count is not None else "-",
            f"{record.rating:.1f}" if record.rating is not None else "-",
            "[green]y[/]" if record.website_url else "[red]n[/]",
            f"[{style}]{outcome.result}[/]" if style else outcome.result,
            _truncate(outcome.headline_reason(), 78),
        )
    return table


def _reason_breakdown(outcomes: list[GateOutcome]) -> Table:
    """Which gate checks are actually doing the cutting. Retuning starts here."""
    tally: dict[str, dict[str, int]] = {}
    for outcome in outcomes:
        for reason in outcome.verdict.reasons:
            bucket = tally.setdefault(reason.label, {"pass": 0, "fail": 0, "unknown": 0})
            bucket[reason.verdict] += 1

    table = Table(title="Check tally", header_style="bold")
    table.add_column("Gate check", width=38)
    table.add_column("pass", justify="right", style="green")
    table.add_column("fail", justify="right", style="red")
    table.add_column("unknown", justify="right", style="yellow")

    for label, counts in sorted(tally.items(), key=lambda kv: -kv[1]["fail"]):
        table.add_row(label, str(counts["pass"]), str(counts["fail"]), str(counts["unknown"]))
    return table


def _summarize(result: SweepResult) -> None:
    counts = result.counts
    console.print(_results_table(result.outcomes))
    console.print(_reason_breakdown(result.outcomes))

    incumbents = [o for o in result.outcomes if o.verdict.incumbent_agency]
    unreachable = [o for o in result.outcomes if o.record.website_url and not (o.signals and o.signals.reachable)]

    console.print()
    console.print(f"[bold]{result.market}[/]  batch [cyan]{result.batch_id}[/]")
    console.print(
        f"  ingested {counts['found']}   suppressed {counts['suppressed']}   gated {counts['gated']}"
    )
    console.print(
        f"  [bold green]pass {counts[GATE_PASS]}[/]   "
        f"[yellow]review {counts[GATE_REVIEW]}[/]   "
        f"[dim red]fail {counts[GATE_FAIL]}[/]   "
        f"[bold]continuing to audit: {len(result.continuing)}[/]"
    )
    if incumbents:
        vendors = sorted({o.verdict.incumbent_agency for o in incumbents if o.verdict.incumbent_agency})
        console.print(f"  incumbent agency footprint on {len(incumbents)}: {', '.join(vendors)}")
    if unreachable:
        console.print(f"  [dim]site listed but unreachable: {len(unreachable)}[/]")

    no_site = [o for o in result.outcomes if not o.record.website_url]
    if no_site:
        console.print(f"  [dim]no website on the profile: {len(no_site)}[/]")


@app.command()
def sweep(
    market: str = typer.Argument(..., help='Metro name, e.g. "Colorado Springs"'),
    limit: int = typer.Option(100, "--limit", "-n", help="Max prospects to ingest."),
    concurrency: int = typer.Option(
        DEFAULT_GATE_CONCURRENCY, "--concurrency", "-c", help="Prospects gated at once."
    ),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="Write gate results to Firestore."),
) -> None:
    """Ingest a metro and run the fit gate on everything that survives suppression."""
    spec = resolve_market(market)
    if not spec.boundaries_known:
        console.print(
            f"[yellow]{spec.name} is not an enumerated metro.[/] The local-operator check will "
            f"report unknown rather than fail. Known metros: {', '.join(known_markets())}."
        )

    console.print(f"Sweeping [bold]{spec.query_market()}[/], limit {limit}…")

    progress: dict[str, int] = {"done": 0, "total": 0}

    def on_ingested(ingest: Any) -> None:
        progress["total"] = len(ingest.records)
        console.print(
            f"  ingested [bold]{ingest.found}[/], {ingest.suppressed} suppressed, "
            f"gating {len(ingest.records)}…"
        )

    def on_gated(outcome: GateOutcome) -> None:
        progress["done"] += 1
        style = RESULT_STYLE.get(outcome.result, "")
        console.print(
            f"  [dim]{progress['done']:>3}/{progress['total']}[/] "
            f"[{style}]{outcome.result:<6}[/] {_truncate(outcome.record.business_name, 38)}",
            highlight=False,
        )

    try:
        result = asyncio.run(
            run_sweep(
                market,
                limit=limit,
                concurrency=concurrency,
                persist=persist,
                on_ingested=on_ingested,
                on_gated=on_gated,
            )
        )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1)

    _summarize(result)


# ── show ──────────────────────────────────────────────────────────────────────


@app.command()
def show(place_id: str = typer.Argument(..., help="Places id, which is the prospect doc id.")) -> None:
    """Print one stored prospect and every gate reason recorded for it."""
    prospect = store.get_prospect(place_id)
    if prospect is None:
        console.print(f"[red]No prospect {place_id}.[/]")
        raise typer.Exit(code=1)

    console.print(f"[bold]{prospect.get('business_name', '?')}[/]  {place_id}")
    for key in ("website_url", "gbp_phone", "site_phone", "address", "review_count", "rating",
                "owner_name", "founded_year", "incumbent_agency", "gate_result", "suppressed_reason"):
        if prospect.get(key) is not None:
            console.print(f"  {key:<18} {prospect[key]}")

    reasons = prospect.get("gate_reasons") or []
    if reasons:
        table = Table(header_style="bold")
        table.add_column("Check", width=34)
        table.add_column("Verdict", width=8)
        table.add_column("Severity", width=9)
        table.add_column("Detail", overflow="fold")
        for reason in reasons:
            colour = {"pass": "green", "fail": "red", "unknown": "yellow"}.get(reason.get("verdict", ""), "")
            table.add_row(
                reason.get("label", ""),
                f"[{colour}]{reason.get('verdict','')}[/]" if colour else reason.get("verdict", ""),
                reason.get("severity", ""),
                reason.get("detail", ""),
            )
        console.print(table)


# ── check definitions ─────────────────────────────────────────────────────────


@app.command("seed-checks")
def seed_checks(
    force: bool = typer.Option(
        False, "--force", help="Overwrite enabled flags and points already tuned in Firestore."
    ),
) -> None:
    """Seed the 44 check definitions into Firestore.

    Firestore is the source of truth once seeded, because the engine spec wants
    a retune to be a document edit rather than a deploy. So a re-run merges the
    descriptive fields and leaves `points` and `enabled` alone unless --force,
    which stops this command from quietly reverting a weight you tuned after the
    first batch.
    """
    existing = {row["code"]: row for row in store.all_check_defs()}
    tuned: list[str] = []
    payload: list[dict[str, Any]] = []

    for definition in CHECK_DEFINITIONS:
        row = dict(definition)
        prior = existing.get(row["code"])
        if prior and not force:
            for field in ("points", "enabled"):
                if field in prior and prior[field] != row[field]:
                    tuned.append(f"{row['code']}.{field} {row[field]} -> {prior[field]}")
                    row[field] = prior[field]
        payload.append(row)

    written = store.upsert_check_defs(payload)

    table = Table(title="Seeded check definitions", header_style="bold")
    table.add_column("Section")
    table.add_column("Checks", justify="right")
    table.add_column("Points", justify="right")
    table.add_column("Enabled points", justify="right")
    for section in ("found", "chosen", "booked", MEASUREMENT):
        rows = [r for r in payload if r["section"] == section]
        table.add_row(
            section,
            str(len(rows)),
            str(section_points(section)),
            str(sum(r["points"] for r in rows if r["enabled"])),
        )
    console.print(table)
    console.print(f"wrote [bold]{written}[/] definitions to Firestore")

    disabled = [r for r in payload if not r["enabled"]]
    if disabled:
        console.print(f"  [dim]not running this week: {', '.join(r['code'] for r in disabled)}[/]")
    if tuned:
        console.print("  [yellow]kept the tuned values already in Firestore:[/]")
        for line in tuned:
            console.print(f"    {line}")
        console.print("  [dim]pass --force to overwrite them with the values in code.[/]")


@app.command("checks")
def list_checks() -> None:
    """List the check definitions as Firestore currently holds them."""
    rows = store.all_check_defs()
    if not rows:
        console.print("[yellow]No check definitions stored. Run: seed-checks[/]")
        raise typer.Exit(code=1)

    table = Table(header_style="bold")
    table.add_column("Code", width=5)
    table.add_column("Section", width=11)
    table.add_column("Title", width=30)
    table.add_column("Pts", justify="right", width=3)
    table.add_column("Full credit", width=42, overflow="fold")
    table.add_column("On", width=3, justify="center")
    for row in rows:
        on = "[green]y[/]" if row.get("enabled") else "[dim red]n[/]"
        table.add_row(
            row.get("code", ""),
            row.get("section", ""),
            _truncate(row.get("title"), 30),
            str(row.get("points", 0)),
            _truncate(row.get("full_credit"), 42),
            on,
        )
    console.print(table)


# ── audit ─────────────────────────────────────────────────────────────────────

STATUS_STYLE = {"pass": "green", "fail": "red", "skipped": "yellow", "error": "bold red"}
STATUS_MARK = {"pass": "pass", "fail": "FAIL", "skipped": "skip", "error": "ERR"}


def _section_table(outcome: AuditOutcome, section: str) -> Table:
    rows = outcome.by_section(section)
    score = outcome.score.sections.get(section)
    title = section.title()
    if score:
        title = (f"{section.title()}  {score.normalized:.0f}/{score.nominal}"
                 f"   [dim]{score.earned} of {score.available} points measured[/]")
        if score.partial:
            title += "  [yellow]partial[/]"

    table = Table(title=title, header_style="bold", title_justify="left")
    table.add_column("", width=4)
    table.add_column("Check", width=26)
    table.add_column("", width=4, justify="center")
    table.add_column("Pts", width=5, justify="right")
    table.add_column("What we saw", overflow="fold")

    for definition, res in rows:
        style = STATUS_STYLE.get(res.status, "")
        awarded = definition.get("points", 0) if res.passed else 0
        table.add_row(
            definition.get("code", ""),
            _truncate(definition.get("title"), 26),
            f"[{style}]{STATUS_MARK.get(res.status, res.status)}[/]",
            f"{awarded}/{definition.get('points', 0)}",
            res.note,
        )
    return table


@app.command()
def audit(
    place_id: str = typer.Argument(..., help="Places id of a prospect already ingested."),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="Write the audit to Firestore."),
    sections: str = typer.Option("found,chosen", "--sections", help="Comma separated, or 'all'."),
) -> None:
    """Run every implemented check against one prospect and score it."""
    prospect = store.get_prospect(place_id)
    if prospect is None:
        console.print(f"[red]No prospect {place_id}. Run a sweep first.[/]")
        raise typer.Exit(code=1)

    definitions = store.all_check_defs()
    if not definitions:
        console.print("[yellow]No check definitions stored. Run: seed-checks[/]")
        raise typer.Exit(code=1)

    market = resolve_market(str(prospect.get("city") or prospect.get("market_id") or ""))
    console.print(
        f"Auditing [bold]{prospect.get('business_name', place_id)}[/]  "
        f"{prospect.get('website_url') or '[dim]no website[/]'}"
    )

    async def go() -> AuditOutcome:
        async with Crawler() as crawler:
            return await audit_one(crawler, prospect, market, definitions, persist=persist)

    outcome = asyncio.run(go())

    wanted = [FOUND, CHOSEN, BOOKED, "measurement"] if sections.strip() == "all" else [
        s.strip() for s in sections.split(",") if s.strip()
    ]
    for section in wanted:
        console.print(_section_table(outcome, section))

    score = outcome.score
    console.print()
    if outcome.crawl_error:
        console.print(f"  [yellow]crawl: {outcome.crawl_error}[/]")
    console.print(
        f"  [bold]total {score.total}/100[/]   band [bold]{score.band}[/]   "
        f"segment [bold]{score.segment or 'unsegmented'}[/]"
        + ("   [yellow]partial: " + ", ".join(score.partial_sections) + "[/]" if score.partial else "")
    )
    for name in (FOUND, CHOSEN, BOOKED):
        row = score.sections[name]
        console.print(
            f"    {name:<7} {row.normalized:>5.1f}/{row.nominal}"
            f"   [dim]measured {row.available} of {row.basis} enabled points,"
            f" coverage {row.coverage:.0%} of nominal[/]"
        )
    if outcome.audit_id:
        console.print(f"  [dim]audit {outcome.audit_id}[/]")


# ── batch dispatch and resume ─────────────────────────────────────────────────

TASK_STYLE = {DONE: "green", RUNNING: "cyan", PENDING: "yellow", FAILED: "red"}


def _stale(task: dict) -> bool:
    """Running, but nobody has renewed the lease. The worker is presumed dead."""
    if task.get("status") != RUNNING:
        return False
    expires = task.get("lease_expires_at")
    return expires is None or expires <= store.utcnow()


@app.command()
def dispatch(
    batch_id: str = typer.Argument(..., help="Batch to fan out."),
    market: str = typer.Option(..., "--market", "-m", help="Metro whose gated prospects to audit."),
    limit: int = typer.Option(0, "--limit", "-n", help="0 means every eligible prospect."),
) -> None:
    """Publish one audit message per gated prospect.

    Only PASS and REVIEW continue, and suppressed prospects never do. The
    message carries nothing but the two ids: everything else is read from
    Firestore by whichever worker picks it up, because a message that carries
    state goes stale the moment it is redelivered.
    """
    market_id = store.market_id_for(resolve_market(market).name)
    eligible = [
        p for p in store.prospects_for_market(market_id, suppressed=False)
        if p.get("gate_result") in (GATE_PASS, GATE_REVIEW)
    ]
    eligible.sort(key=lambda p: -(p.get("review_count") or 0))
    if limit:
        eligible = eligible[:limit]

    if not eligible:
        console.print(f"[yellow]No gated prospects in {market_id}. Run a sweep first.[/]")
        raise typer.Exit(code=1)

    console.print(f"Publishing [bold]{len(eligible)}[/] audits for batch [cyan]{batch_id}[/]…")
    ids = [p["place_id"] for p in eligible]
    seeded = seed_tasks(batch_id, ids)
    console.print(f"  seeded {seeded} pending tasks in the ledger")
    published = publish_batch(batch_id, ids)
    console.print(f"  published [bold green]{published}[/] messages to the audit topic")
    console.print(f"  watch with: [dim]python -m app.cli batch {batch_id}[/]")


@app.command()
def batch(
    batch_id: str = typer.Argument(..., help="Batch to report on."),
    show: int = typer.Option(0, "--show", help="List this many task rows."),
) -> None:
    """Where a batch has got to, read from the task ledger."""
    tasks = tasks_for_batch(batch_id)
    if not tasks:
        console.print(f"[yellow]No tasks recorded for {batch_id}.[/]")
        raise typer.Exit(code=1)

    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.get("status") or PENDING] = counts.get(task.get("status") or PENDING, 0) + 1
    stale = [t for t in tasks if _stale(t)]
    exhausted = [t for t in tasks if (t.get("attempts") or 0) >= MAX_ATTEMPTS
                 and t.get("status") != DONE]

    table = Table(title=f"batch {batch_id}", header_style="bold", title_justify="left")
    table.add_column("Status", width=10)
    table.add_column("Count", justify="right", width=6)
    for status in (DONE, RUNNING, PENDING, FAILED):
        if counts.get(status):
            table.add_row(f"[{TASK_STYLE[status]}]{status}[/]", str(counts[status]))
    console.print(table)

    total = len(tasks)
    done = counts.get(DONE, 0)
    console.print(f"  [bold]{done}/{total}[/] complete" + ("  [bold green]batch finished[/]" if done == total else ""))
    if stale:
        console.print(f"  [yellow]{len(stale)} lease(s) lapsed, the holder is presumed dead[/]")
    if exhausted:
        console.print(f"  [red]{len(exhausted)} at the attempt ceiling, see the dead letter topic[/]")

    if show:
        rows = Table(header_style="bold")
        rows.add_column("Prospect", width=30)
        rows.add_column("Status", width=8)
        rows.add_column("Try", justify="right", width=3)
        rows.add_column("Total", justify="right", width=5)
        rows.add_column("Worker", width=26, overflow="fold")
        for task in sorted(tasks, key=lambda t: (t.get("status") or "", t.get("prospect_id") or ""))[:show]:
            status = task.get("status") or PENDING
            rows.add_row(
                _truncate(task.get("prospect_id"), 30),
                f"[{TASK_STYLE.get(status, '')}]{status}[/]",
                str(task.get("attempts") or 0),
                str(task.get("total") if task.get("total") is not None else "-"),
                _truncate(task.get("lease_owner") or task.get("last_worker"), 26),
            )
        console.print(rows)


@app.command()
def resume(
    batch_id: str = typer.Argument(..., help="Batch to resume."),
    force: bool = typer.Option(False, "--force", help="Also retry tasks at the attempt ceiling."),
) -> None:
    """Republish everything that has not finished.

    Pub/Sub redelivers on its own, so this is for the cases it cannot see: a
    message that was acked while contended, a subscription recreated, or a
    worker killed after the last delivery. Completed prospects are never
    republished, and a duplicate delivery is harmless anyway because the claim
    is what decides, not the message.
    """
    tasks = tasks_for_batch(batch_id)
    if not tasks:
        console.print(f"[yellow]No tasks recorded for {batch_id}.[/]")
        raise typer.Exit(code=1)

    unfinished = []
    for task in tasks:
        if task.get("status") == DONE:
            continue
        if (task.get("attempts") or 0) >= MAX_ATTEMPTS and not force:
            continue
        if task.get("status") == RUNNING and not _stale(task):
            continue  # someone is actively on it
        unfinished.append(task["prospect_id"])

    if not unfinished:
        done = sum(1 for t in tasks if t.get("status") == DONE)
        console.print(f"[green]Nothing to resume. {done}/{len(tasks)} complete.[/]")
        return

    console.print(f"Republishing [bold]{len(unfinished)}[/] unfinished prospects…")
    published = publish_batch(batch_id, unfinished)
    console.print(f"  published [bold green]{published}[/] messages")


# ── rescore ───────────────────────────────────────────────────────────────────


@app.command()
def rescore(
    batch_id: str = typer.Argument(..., help="Batch whose audits to rescore."),
) -> None:
    """Recompute scores and segments from stored check results.

    This is why check definitions live in Firestore: a retune is a document
    edit plus a rescore, not a re-crawl. Checks that were run and later
    disabled are ignored; checks enabled since are counted as skipped, which
    keeps the partial flag honest about what this audit actually measured.
    """
    definitions = store.all_check_defs()
    audits = list(store.audits_for_batch(batch_id))
    if not audits:
        console.print(f"[yellow]No audits for batch {batch_id}.[/]")
        raise typer.Exit(code=1)

    moved = 0
    for audit in audits:
        checks = store.audit_checks(audit["audit_id"])
        statuses = {c["code"]: c["status"] for c in checks if c.get("code")}
        score = compute(outcomes_from(statuses, definitions))
        changed = (
            audit.get("scores", {}).get("total") != score.total
            or audit.get("segment") != score.segment
            or bool(audit.get("partial")) != score.partial
        )
        if changed:
            moved += 1
        store.update_audit(audit["audit_id"], {
            "scores": {"found": score.found, "chosen": score.chosen,
                       "booked": score.booked, "total": score.total},
            "band": score.band,
            "segment": score.segment,
            "partial": score.partial,
            "partial_sections": list(score.partial_sections),
            "rescored_at": store.utcnow(),
        })
    console.print(f"rescored [bold]{len(audits)}[/] audits, [bold]{moved}[/] changed")


# ── call list ─────────────────────────────────────────────────────────────────

SEGMENT_STYLE = {"Leaky Bucket": "bold orange1", "Invisible Pro": "bold cyan",
                 "Both Broken": "bold red", "Dialed": "bold green"}


def _load_ranked(batch_id: str):
    audits = list(store.audits_for_batch(batch_id))
    if not audits:
        console.print(f"[yellow]No audits recorded for batch {batch_id}.[/]")
        raise typer.Exit(code=1)
    prospects = {}
    for audit in audits:
        pid = audit.get("prospect_id")
        if pid and pid not in prospects:
            prospects[pid] = store.get_prospect(pid) or {}
    return rank(audits, prospects)


@app.command("call-list")
def call_list(
    batch_id: str = typer.Argument(..., help="Batch to rank."),
    draft: int = typer.Option(0, "--draft", help="Draft findings for the top N prospects."),
) -> None:
    """The ranked call list: segment priority first, emptiest bucket first.

    With --draft, the diagnostician drafts three findings per top prospect.
    Drafts only. A human approves before anything becomes a report, and
    suppression is checked before every draft.
    """
    rows = _load_ranked(batch_id)

    for segment, group in by_segment(rows):
        style = SEGMENT_STYLE.get(segment, "dim")
        table = Table(
            title=f"[{style}]{segment}[/]  ({len(group)})",
            header_style="bold", title_justify="left",
        )
        table.add_column("#", justify="right", width=4)
        table.add_column("Business", width=34)
        table.add_column("City", width=16)
        table.add_column("F", justify="right", width=3)
        table.add_column("C", justify="right", width=3)
        table.add_column("B", justify="right", width=3)
        table.add_column("Total", justify="right", width=5)
        table.add_column("Phone", width=15)
        table.add_column("", width=10)
        for row in group:
            scores = row.scores
            tags = []
            if row.incumbent_agency:
                tags.append("[magenta]agency[/]")
            if row.partial:
                tags.append("[yellow]partial[/]")
            table.add_row(
                str(row.rank), _truncate(row.business_name, 34), _truncate(row.city, 16),
                str(scores.get("found", "-")), str(scores.get("chosen", "-")),
                str(scores.get("booked", "-")), str(scores.get("total", "-")),
                row.phone or "-", " ".join(tags),
            )
        console.print(table)

    if draft:
        _draft_top(rows[:draft])


def _draft_top(rows) -> None:
    import asyncio as _asyncio

    from app.agents.diagnostician import draft_findings

    suppressions = store.load_suppressions()
    definitions = {d["code"]: d for d in store.all_check_defs()}

    async def draft_one(row):
        prospect = store.get_prospect(row.prospect_id) or {}
        # Rule 3: suppression is checked before every outreach action, drafts
        # included, and matched on every identifier we hold.
        hit = store.suppression_hit(
            suppressions,
            place_id=row.prospect_id,
            domain=prospect.get("domain"),
            phone=prospect.get("gbp_phone"),
            email=prospect.get("owner_email"),
        )
        if hit:
            return row, None, f"suppressed ({hit})"
        checks = store.audit_checks(row.audit_id)
        failures = [
            {**c, "title": definitions.get(c.get("code"), {}).get("title"),
             "points": definitions.get(c.get("code"), {}).get("points", 0)}
            for c in checks if c.get("status") == "fail"
        ]
        failures.sort(key=lambda f: -f["points"])
        diagnosis = await draft_findings(
            business_name=row.business_name, city=row.city or "",
            failures=failures,
        )
        if not diagnosis.ok:
            return row, None, diagnosis.error
        store.save_draft_findings(
            row.audit_id, [f.to_dict() for f in diagnosis.findings],
            needs_review=diagnosis.needs_review, model=diagnosis.model,
        )
        return row, diagnosis, None

    async def run_all():
        return await _asyncio.gather(*(draft_one(r) for r in rows))

    console.print()
    console.print(f"[bold]Drafting findings for the top {len(rows)}…[/] "
                  "(drafts only, a human approves)")
    for row, diagnosis, error in _asyncio.run(run_all()):
        console.print(f"\n[bold]{row.rank}. {row.business_name}[/]  "
                      f"[{SEGMENT_STYLE.get(row.segment or '', 'dim')}]{row.segment or 'incomplete'}[/]")
        if error:
            console.print(f"   [yellow]no draft: {error}[/]")
            continue
        for f in diagnosis.findings:
            review = " [yellow](flagged for review)[/]" if f.mechanism_flags else ""
            console.print(f"   [bold]{f.ordinal}. {f.check_code}[/]{review}")
            console.print(f"      saw:   {f.what_we_saw}")
            console.print(f"      means: {f.what_it_means}")
            console.print(f"      fix:   {f.what_fixing_takes}")


if __name__ == "__main__":
    app()
