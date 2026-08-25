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

from app.config import ConfigError, get_config
from app.gate import GATE_FAIL, GATE_PASS, GATE_REVIEW
from app.markets import known_markets, resolve_market
from app.pipeline import DEFAULT_GATE_CONCURRENCY, GateOutcome, SweepResult, run_sweep
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


if __name__ == "__main__":
    app()
