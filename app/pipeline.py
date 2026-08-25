"""Sweep orchestration: ingest a metro, then gate every prospect.

This is the day-one pipeline, written as plain async functions rather than ADK
agents on purpose. The coordinator wraps these on Thursday; keeping the work in
callable functions means the fan-out change is a transport change, not a
rewrite, and means the whole sweep stays runnable from a terminal with no
Pub/Sub in the loop.

Nothing here invents a value. A prospect with no website gets a gate verdict
built from Places alone and reasons that say so.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import get_config
from app.gate import GATE_FAIL, GATE_PASS, GATE_REVIEW, GateInput, GateVerdict, evaluate
from app.markets import MarketSpec, resolve_market
from app.store import firestore as store
from app.tools.crawl import GATE_PRIORITY_FRAGMENTS, Crawler, SiteCrawl
from app.tools.places import PlaceRecord, ingest_market
from app.tools.site_signals import SiteSignals, extract_signals

# The gate asks whether this is a real, established, residential operator. That
# lives on the about, team, careers and contact pages, not the homepage alone.
# The engine spec says "a single homepage fetch"; the criteria doc's gate needs
# tenure, a named crew and an owner name, and the criteria doc wins on what is
# measured. Six pages is the smallest budget that reliably reaches them.
GATE_PAGE_BUDGET = 6

GATE_EXTRA_PATHS: tuple[str, ...] = (
    "/about",
    "/about-us",
    "/our-team",
    "/contact",
    "/careers",
)

# One slow host must not hold the sweep open. Well past a 15s page timeout
# across a six page budget, but bounded.
PER_PROSPECT_TIMEOUT_SECONDS = 120.0

# Distinct hosts, so this is not a politeness number. Per-host limits are
# enforced separately and independently inside the shared Crawler.
DEFAULT_GATE_CONCURRENCY = 8


@dataclass
class GateOutcome:
    record: PlaceRecord
    verdict: GateVerdict
    signals: SiteSignals | None = None
    crawl_error: str | None = None

    @property
    def result(self) -> str:
        return self.verdict.result

    @property
    def blocking_failures(self) -> list[str]:
        return [r.label for r in self.verdict.reasons if r.verdict == "fail" and r.severity == "blocking"]

    def headline_reason(self) -> str:
        """The one line an operator reads in the call list."""
        fails = [r for r in self.verdict.reasons if r.verdict == "fail"]
        if fails:
            blocking = [r for r in fails if r.severity == "blocking"]
            return (blocking or fails)[0].detail
        unknowns = [r for r in self.verdict.reasons if r.verdict == "unknown"]
        if unknowns:
            return unknowns[0].detail
        return "All gate checks passed."


@dataclass
class SweepResult:
    market: str
    market_id: str
    batch_id: str
    found: int
    suppressed: int
    outcomes: list[GateOutcome] = field(default_factory=list)

    def by_result(self, result: str) -> list[GateOutcome]:
        return [o for o in self.outcomes if o.result == result]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "found": self.found,
            "suppressed": self.suppressed,
            "gated": len(self.outcomes),
            GATE_PASS: len(self.by_result(GATE_PASS)),
            GATE_REVIEW: len(self.by_result(GATE_REVIEW)),
            GATE_FAIL: len(self.by_result(GATE_FAIL)),
        }

    @property
    def continuing(self) -> list[GateOutcome]:
        """PASS and REVIEW. These are the ones an audit runs on."""
        return [o for o in self.outcomes if o.verdict.continues]


async def _crawl_for_gate(crawler: Crawler, url: str) -> tuple[SiteCrawl | None, str | None]:
    try:
        crawl = await asyncio.wait_for(
            crawler.crawl_site(
                url,
                max_pages=GATE_PAGE_BUDGET,
                extra_paths=GATE_EXTRA_PATHS,
                priority_fragments=GATE_PRIORITY_FRAGMENTS,
                # The gate reads pages, it does not need to discover the site.
                # Skipping the sitemap saves one to three requests per host,
                # which is real money across a hundred hosts.
                fetch_sitemap=False,
            ),
            timeout=PER_PROSPECT_TIMEOUT_SECONDS,
        )
        return crawl, None
    except asyncio.TimeoutError:
        return None, f"timed out after {PER_PROSPECT_TIMEOUT_SECONDS:.0f}s"
    except Exception as exc:  # noqa: BLE001 - one bad host must not end the sweep
        return None, f"{type(exc).__name__}: {exc}"


async def gate_one(
    crawler: Crawler,
    record: PlaceRecord,
    market: MarketSpec,
    *,
    persist: bool = True,
) -> GateOutcome:
    """Crawl what exists, evaluate the gate, write the verdict."""
    signals: SiteSignals | None = None
    crawl_error: str | None = None

    if record.website_url:
        crawl, crawl_error = await _crawl_for_gate(crawler, record.website_url)
        if crawl is not None:
            signals = extract_signals(crawl)

    verdict = evaluate(
        GateInput(
            place_id=record.place_id,
            business_name=record.business_name,
            market=market,
            website_url=record.website_url,
            review_count=record.review_count,
            rating=record.rating,
            first_review_at=record.first_review_at,
            latest_review_at=record.latest_review_at,
            review_sample_size=record.review_sample_size,
            gbp_phone=record.gbp_phone,
            city=record.city,
            state=record.state,
            address=record.address,
            primary_type=record.primary_type,
            types=tuple(record.types),
            business_status=record.business_status,
            site=signals,
            # No active client territories are recorded yet, so this always
            # passes. When they exist they belong in Firestore, and an overlap
            # is a blocking fail rather than a suppression, because the
            # prospect is fine and the conflict is ours.
            territory_conflict=False,
        )
    )

    outcome = GateOutcome(record=record, verdict=verdict, signals=signals, crawl_error=crawl_error)

    if persist:
        await asyncio.to_thread(
            store.set_gate_result, record.place_id, verdict.result, verdict.to_dicts()
        )
        site_fields = _site_fields(signals, crawl_error)
        if verdict.incumbent_agency:
            site_fields["incumbent_agency"] = verdict.incumbent_agency
        if site_fields:
            await asyncio.to_thread(store.upsert_prospect, record.place_id, site_fields)

    return outcome


def _site_fields(signals: SiteSignals | None, crawl_error: str | None) -> dict[str, Any]:
    """Only what we actually observed. Absent stays absent."""
    fields: dict[str, Any] = {}
    if crawl_error:
        fields["crawl_error"] = crawl_error
    if signals is None:
        return fields
    fields["site_reachable"] = signals.reachable
    fields["pages_crawled"] = signals.pages_crawled
    if signals.robots_blocked:
        fields["robots_blocked"] = True
    if signals.site_phones:
        fields["site_phone"] = signals.site_phones[0]
        fields["site_phones"] = signals.site_phones
    for key in ("owner_name", "founded_year", "years_in_business", "office_address", "copyright_year"):
        value = getattr(signals, key)
        if value is not None:
            fields[key] = value
    return fields


async def gate_prospects(
    records: Sequence[PlaceRecord],
    market: MarketSpec,
    *,
    concurrency: int = DEFAULT_GATE_CONCURRENCY,
    persist: bool = True,
    on_done: Any = None,
) -> list[GateOutcome]:
    """Gate every record. One failure never takes down the batch."""
    semaphore = asyncio.Semaphore(concurrency)
    outcomes: list[GateOutcome] = []

    async with Crawler() as crawler:

        async def worker(record: PlaceRecord) -> GateOutcome:
            async with semaphore:
                outcome = await gate_one(crawler, record, market, persist=persist)
            if on_done is not None:
                on_done(outcome)
            return outcome

        for task in asyncio.as_completed([worker(r) for r in records]):
            outcomes.append(await task)

    return outcomes


async def run_sweep(
    market_name: str,
    *,
    limit: int = 100,
    concurrency: int = DEFAULT_GATE_CONCURRENCY,
    persist: bool = True,
    on_ingested: Any = None,
    on_gated: Any = None,
) -> SweepResult:
    """Ingest a metro and gate everything that survives suppression."""
    get_config().require("project", "places_api_key")

    market = resolve_market(market_name)
    market_id = await asyncio.to_thread(store.upsert_market, market.name)
    batch_id = await asyncio.to_thread(store.create_batch, market_id, f"sweep {market.name}")

    ingested = await ingest_market(
        market.query_market(),
        batch_id=batch_id,
        market_id=market_id,
        limit=limit,
    )
    if on_ingested is not None:
        on_ingested(ingested)

    outcomes = await gate_prospects(
        ingested.records,
        market,
        concurrency=concurrency,
        persist=persist,
        on_done=on_gated,
    )

    if persist:
        await asyncio.to_thread(store.bump_batch_counts, batch_id, gated=len(outcomes))
        await asyncio.to_thread(store.complete_batch, batch_id, "gated")

    return SweepResult(
        market=market.name,
        market_id=market_id,
        batch_id=batch_id,
        found=ingested.found,
        suppressed=ingested.suppressed,
        outcomes=outcomes,
    )
