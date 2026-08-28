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
from typing import Any, Mapping, Sequence

from app.checks import extract as facts
from app.checks.base import AuditContext, CheckResult, run_checks, statuses
from app.config import get_config
from app.gate import GATE_FAIL, GATE_PASS, GATE_REVIEW, GateInput, GateVerdict, evaluate
from app.markets import MarketSpec, resolve_market
from app.store import firestore as store
from app.scoring import Score, compute, outcomes_from
from app.tools.crawl import (
    GATE_PRIORITY_FRAGMENTS,
    Crawler,
    FetchResult,
    SiteCrawl,
    normalize_url,
)
from app.agents.vision import VisionVerdict, read_screenshot
from app.tools.pagespeed import PsiResult, analyze
from app.tools.places import PlaceRecord, ingest_market
from app.tools.render import RenderResult, render
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

# A full audit crawls 25 pages rather than the gate's six, so it gets longer.
AUDIT_TIMEOUT_SECONDS = 300.0


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

    from app.tools.places import queries_for_market

    ingested = await ingest_market(
        market.query_market(),
        batch_id=batch_id,
        market_id=market_id,
        limit=limit,
        queries=queries_for_market(market),
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


# ── Audit ─────────────────────────────────────────────────────────────────────


@dataclass
class AuditOutcome:
    prospect: Mapping[str, Any]
    audit_id: str | None
    score: Score
    results: dict[str, CheckResult]
    definitions: list[Mapping[str, Any]]
    crawl_error: str | None = None

    def by_section(self, section: str) -> list[tuple[Mapping[str, Any], CheckResult]]:
        """Definitions joined to results, in display order."""
        index = {str(d["code"]): d for d in self.definitions}
        rows = [
            (index[code], res)
            for code, res in self.results.items()
            if code in index and index[code].get("section") == section
        ]
        return sorted(rows, key=lambda row: row[0].get("sort_order", 0))




def _canonical_homepage(crawl: SiteCrawl) -> str | None:
    """The homepage URL without campaign tags.

    A redirect can hand the tags straight back, so the final URL is normalized
    again rather than trusted. Both inspector stages must agree on this string:
    it is the provider cache key and it is what a report cites as evidence.
    """
    return normalize_url(crawl.homepage.final_url or crawl.base_url or "")


async def _render_homepage(crawl: SiteCrawl) -> RenderResult | None:
    """Render the homepage at a mobile viewport, or return None.

    Guardrail 6 lives here rather than in the renderer. The renderer is
    stateless and renders what it is told to, so the decision about whether we
    are allowed to look belongs to the caller that already fetched robots.txt
    during recon. A disallowed homepage is never rendered, and the checks that
    needed it skip and say so.
    """
    if not get_config().renderer_url:
        return None
    if crawl.homepage.blocked_by_robots:
        return None
    if not crawl.reachable:
        return None
    target = _canonical_homepage(crawl)
    if not target:
        return None
    try:
        # A clipped full page rather than the fold: the vision read wants the
        # whole story. form_health rides along in case the lead form is on
        # the homepage itself.
        return await render(target, screenshot="full", image_format="jpeg", form_health=True)
    except Exception as exc:  # noqa: BLE001 - a renderer fault skips its checks
        return RenderResult(ok=False, url=target, error=f"{type(exc).__name__}: {exc}")



async def _measure_speed(crawl: SiteCrawl) -> PsiResult | None:
    """PageSpeed Insights on the homepage.

    Google fetches the page itself here rather than us, but the robots guard
    still applies: if we were told not to look at this page we do not go asking
    a third party to look at it either.
    """
    if not get_config().pagespeed_api_key:
        return None
    if crawl.homepage.blocked_by_robots or not crawl.reachable:
        return None
    target = _canonical_homepage(crawl)
    if not target:
        return None
    try:
        return await analyze(target)
    except Exception as exc:  # noqa: BLE001 - a provider fault skips its checks
        return PsiResult(ok=False, url=target, error=f"{type(exc).__name__}: {exc}")




async def _render_form_page(crawl: SiteCrawl, site: "facts.SiteFacts") -> RenderResult | None:
    """Render the page that actually carries the lead form, for B2.

    The engine spec's render stage is "homepage and contact page". The form is
    found from the crawl first so we render the right page, not a guessed one,
    and skip the second render entirely when the form is on the homepage.
    """
    if not get_config().renderer_url or not crawl.reachable:
        return None
    from app.checks.base import AuditContext as _Ctx
    from app.checks.onpage import _primary_form

    form, page = _primary_form(_Ctx(place={}, site=site))
    if form is None or page is None:
        return None
    homepage = normalize_url(crawl.homepage.final_url or crawl.base_url or "")
    target = normalize_url(page.url)
    if not target or target == homepage:
        return None  # the homepage render already probed it
    if any(target in blocked or page.url in blocked for blocked in crawl.robots_blocked):
        return None
    try:
        return await render(target, screenshot="none", form_health=True)
    except Exception as exc:  # noqa: BLE001
        return RenderResult(ok=False, url=target, error=f"{type(exc).__name__}: {exc}")


async def _read_homepage(rendered: RenderResult | None) -> VisionVerdict | None:
    """Hand the screenshot to the vision component.

    This is the one stage that cannot run beside the render, because it is the
    render's output. PageSpeed still runs alongside both.
    """
    if rendered is None or not rendered.usable:
        # Includes the bot challenge case. Sending an interstitial to the model
        # costs money to be told the page is a captcha.
        return None
    image = rendered.screenshot()
    if not image:
        return None
    try:
        return await read_screenshot(image, mime_type=rendered.screenshot_mime)
    except Exception as exc:  # noqa: BLE001 - a model fault skips its checks
        return VisionVerdict(ok=False, error=f"{type(exc).__name__}: {exc}")


async def persist_audit(
    *,
    prospect: Mapping[str, Any],
    batch_id: str,
    score: Score,
    results: dict[str, CheckResult],
    definitions: list[Mapping[str, Any]],
    crawl_error: str | None,
    pages_crawled: int,
    render: RenderResult | None = None,
) -> str:
    """One write path for an audit, whoever ran it. The ADK graph and the plain
    pipeline must be indistinguishable in Firestore, or resumption and ranking
    would depend on which code path did the work."""
    place_id = str(prospect.get("place_id"))
    audit_id = await asyncio.to_thread(store.create_audit, place_id, batch_id)
    points = {str(d["code"]): int(d.get("points", 0)) for d in definitions}
    rows = []
    for code, res in results.items():
        row = res.to_dict()
        row["points_awarded"] = points.get(code, 0) if res.passed else 0
        rows.append(row)
    await asyncio.to_thread(store.write_check_results, audit_id, rows)

    # The homepage screenshot is the evidence most findings point at. Uploading
    # it here rather than at publish time means it exists for every audit, so
    # guardrail 10 has something to check against.
    if render is not None and render.ok:
        image = render.screenshot()
        if image:
            from app.store import evidence as evidence_store

            try:
                await asyncio.to_thread(
                    evidence_store.upload,
                    place_id, audit_id,
                    f"homepage.{'jpg' if render.screenshot_format == 'jpeg' else 'png'}",
                    image,
                    content_type=render.screenshot_mime,
                    code="C17", kind="screenshot",
                )
            except Exception as exc:  # noqa: BLE001 - evidence is additive, not fatal
                await asyncio.to_thread(
                    store.update_audit, audit_id,
                    {"evidence_error": f"{type(exc).__name__}: {exc}"[:200]},
                )
    await asyncio.to_thread(
        store.update_audit,
        audit_id,
        {
            "status": "scored",
            "scores": {
                "found": score.found, "chosen": score.chosen,
                "booked": score.booked, "total": score.total,
            },
            "band": score.band,
            "segment": score.segment,
            "partial": score.partial,
            "partial_sections": list(score.partial_sections),
            "crawl_error": crawl_error,
            "pages_crawled": pages_crawled,
            "finished_at": store.utcnow(),
        },
    )
    return audit_id




async def audit_one(
    crawler: Crawler,
    prospect: Mapping[str, Any],
    market: MarketSpec,
    definitions: list[Mapping[str, Any]],
    *,
    batch_id: str = "manual",
    persist: bool = True,
) -> AuditOutcome:
    """Recon, run every enabled check, score. Stage failures skip, they do not abort."""
    website = prospect.get("website_url")
    crawl: SiteCrawl | None = None
    crawl_error: str | None = None

    if website:
        try:
            crawl = await asyncio.wait_for(
                crawler.crawl_site(website), timeout=AUDIT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            crawl_error = f"crawl timed out after {AUDIT_TIMEOUT_SECONDS:.0f}s"
        except Exception as exc:  # noqa: BLE001 - a bad host skips its checks
            crawl_error = f"{type(exc).__name__}: {exc}"
    else:
        crawl_error = "no website on the Google profile"

    if crawl is None:
        crawl = SiteCrawl(base_url=website or "", homepage=FetchResult(url=website or "", error=crawl_error))

    # The inspector stages are independent and I/O bound, which is exactly why
    # the engine spec models them as a ParallelAgent. Sequentially this is a
    # nine second render followed by a thirty second Lighthouse run.
    site_facts = facts.build(crawl)

    async def look() -> tuple[RenderResult | None, VisionVerdict | None]:
        rendered = await _render_homepage(crawl)
        return rendered, await _read_homepage(rendered)

    (render_result, vision_result), psi_result, form_render = await asyncio.gather(
        look(), _measure_speed(crawl), _render_form_page(crawl, site_facts)
    )

    # B2 reads the render of whichever page carries the form. When the form is
    # on the homepage, that is the homepage render itself.
    if form_render is None and render_result is not None:
        form_render = render_result

    # A JavaScript built site serves almost no text. Fold the rendered homepage
    # into the facts so the text based checks read what a homeowner reads.
    site_facts = facts.with_rendered_homepage(site_facts, render_result)

    ctx = AuditContext(
        place=dict(prospect),
        site=site_facts,
        market=market,
        render=render_result,
        form_render=form_render,
        psi=psi_result,
        vision=vision_result,
    )
    results = run_checks(ctx, definitions)
    score = compute(outcomes_from(statuses(results), definitions))

    audit_id: str | None = None
    if persist:
        audit_id = await persist_audit(
            prospect=prospect, batch_id=batch_id, score=score, results=results,
            definitions=definitions, crawl_error=crawl_error,
            pages_crawled=len(ctx.site.pages), render=render_result,
        )

    return AuditOutcome(
        prospect=prospect,
        audit_id=audit_id,
        score=score,
        results=results,
        definitions=definitions,
        crawl_error=crawl_error,
    )
