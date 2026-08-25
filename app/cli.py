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
from app.scoring import BOOKED, CHOSEN, FOUND
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


if __name__ == "__main__":
    app()
