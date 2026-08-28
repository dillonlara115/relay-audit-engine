"""The audit agent, as the engine spec draws it.

    audit_agent (SequentialAgent)
      ├── recon (custom agent)          robots → homepage → sitemap → key pages
      ├── inspector (ParallelAgent)     onpage | speed | vision
      ├── booked + score (custom)       checks over everything gathered, then math
      └── (diagnostician runs at rank time, once a human is in the loop)

recon feeds the inspector, so they are sequential. The inspector's three reads
are independent and I/O bound, so they are parallel. Neither carries a model:
per the house rule only four components do, and two of them (vision inside the
inspector, diagnostician at rank time) are elsewhere in this codebase.

Each stage is a thin BaseAgent over the same functions the tests already pin
down. State crosses stages through session.state, which is the ADK contract,
and the stages stash Python objects there rather than re-serializing work the
next stage immediately needs.
"""

from __future__ import annotations

import time
from typing import Any, AsyncGenerator, Mapping

from google.adk.agents import BaseAgent, ParallelAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types as genai_types

from app.checks import extract as facts
from app.checks.base import AuditContext, run_checks, statuses
from app.pipeline import (
    AUDIT_TIMEOUT_SECONDS,
    _measure_speed,
    _read_homepage,
    _render_form_page,
    _render_homepage,
)
from app.scoring import compute, outcomes_from
from app.tools.crawl import Crawler, FetchResult, SiteCrawl


def _note(agent: str, text: str) -> Event:
    """A progress event. The transcript is the audit's flight recorder."""
    return Event(
        author=agent,
        content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]),
    )


class ReconAgent(BaseAgent):
    """robots → homepage → sitemap → key pages, in that order, politely."""

    name: str = "recon"
    description: str = "Crawls the prospect site within the politeness rules."

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        import asyncio

        state = ctx.session.state
        website = (state.get("prospect") or {}).get("website_url")
        started = time.monotonic()

        crawl: SiteCrawl | None = None
        error: str | None = None
        if website:
            try:
                async with Crawler() as crawler:
                    crawl = await asyncio.wait_for(
                        crawler.crawl_site(website), timeout=AUDIT_TIMEOUT_SECONDS
                    )
            except asyncio.TimeoutError:
                error = f"crawl timed out after {AUDIT_TIMEOUT_SECONDS:.0f}s"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
        else:
            error = "no website on the Google profile"

        if crawl is None:
            crawl = SiteCrawl(base_url=website or "",
                              homepage=FetchResult(url=website or "", error=error))

        state["crawl"] = crawl
        state["site_facts"] = facts.build(crawl)
        state["crawl_error"] = error
        pages = len(state["site_facts"].pages)
        yield _note(self.name,
                    f"recon: {pages} pages in {time.monotonic() - started:.1f}s"
                    + (f" ({error})" if error else ""))


class _InspectorBranch(BaseAgent):
    """One independent read. Subclasses set the stage."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        crawl: SiteCrawl = state["crawl"]
        result = await self._inspect(crawl, state)
        state[self.name] = result
        yield _note(self.name, self._describe(result))

    async def _inspect(self, crawl: SiteCrawl, state: Mapping[str, Any]) -> Any:
        raise NotImplementedError

    def _describe(self, result: Any) -> str:
        return f"{self.name}: {'ok' if result is not None else 'nothing to read'}"


class LookAgent(_InspectorBranch):
    """Render the homepage, then let the vision model read the screenshot.

    Sequential inside this branch because vision consumes the render. Still
    parallel to the other branches, which is the point of the fan.
    """

    name: str = "look"
    description: str = "Mobile render plus the vision trust read."

    async def _inspect(self, crawl: SiteCrawl, state: Any) -> Any:
        rendered = await _render_homepage(crawl)
        vision = await _read_homepage(rendered)
        state["render"] = rendered
        state["vision"] = vision
        return rendered

    def _describe(self, rendered: Any) -> str:
        if rendered is None:
            return "look: nothing to render"
        vision_ok = getattr(self.__dict__.get("_last_vision"), "ok", None)
        return (f"look: rendered {getattr(rendered, 'status', None)}, "
                f"screenshot {'yes' if getattr(rendered, 'screenshot_bytes', None) else 'no'}")


class SpeedAgent(_InspectorBranch):
    name: str = "speed"
    description: str = "PageSpeed Insights, mobile."

    async def _inspect(self, crawl: SiteCrawl, state: Any) -> Any:
        result = await _measure_speed(crawl)
        state["psi"] = result
        return result

    def _describe(self, psi: Any) -> str:
        if psi is None or not getattr(psi, "ok", False):
            return "speed: not measured"
        return f"speed: score {psi.performance_score}, LCP {psi.lcp_ms and round(psi.lcp_ms)}ms"


class FormProbeAgent(_InspectorBranch):
    name: str = "form_probe"
    description: str = "Renders the page carrying the lead form. Fills, never submits."

    async def _inspect(self, crawl: SiteCrawl, state: Any) -> Any:
        result = await _render_form_page(crawl, state["site_facts"])
        state["form_render"] = result
        return result

    def _describe(self, result: Any) -> str:
        if result is None:
            return "form_probe: form is on the homepage or absent"
        health = getattr(result, "form_health", None) or {}
        return f"form_probe: found={health.get('found')} visible={health.get('visible')}"


class ScoreAgent(BaseAgent):
    """Every enabled check, then the pure scoring function."""

    name: str = "score"
    description: str = "Runs the check battery and computes section scores."

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        form_render = state.get("form_render") or state.get("render")
        site_facts = facts.with_rendered_homepage(
            state["site_facts"], state.get("render")
        )
        state["site_facts"] = site_facts
        audit_ctx = AuditContext(
            place=dict(state.get("prospect") or {}),
            site=site_facts,
            market=state.get("market"),
            render=state.get("render"),
            form_render=form_render,
            psi=state.get("psi"),
            vision=state.get("vision"),
        )
        definitions = state["definitions"]
        results = run_checks(audit_ctx, definitions)
        score = compute(outcomes_from(statuses(results), definitions))
        state["results"] = results
        state["score"] = score

        # The session service persists state by value, but the worker needs the
        # live objects. deliver() is a closure the caller planted in the state:
        # deepcopy treats functions atomically, so it reaches the caller's
        # holder no matter how many times the session was copied in between.
        deliver = state.get("_deliver")
        if callable(deliver):
            deliver(
                score=score,
                results=results,
                site_facts=state["site_facts"],
                crawl_error=state.get("crawl_error"),
                render=state.get("render"),
            )
        yield _note(
            self.name,
            f"score: total {score.total} ({score.band}), "
            f"F {score.normalized('found'):.0f} C {score.normalized('chosen'):.0f} "
            f"B {score.normalized('booked'):.0f}, segment {score.segment or 'none'}",
        )


def build_audit_agent() -> SequentialAgent:
    """The graph in the engine spec, assembled fresh per run.

    ADK agents are single-parent, so a shared module-level instance cannot be
    mounted twice. Building per call keeps the graph reusable and the state
    isolated to one session.
    """
    inspector = ParallelAgent(
        name="inspector",
        description="The three independent reads, fanned out.",
        sub_agents=[LookAgent(), SpeedAgent(), FormProbeAgent()],
    )
    return SequentialAgent(
        name="audit_agent",
        description="Recon, inspect in parallel, then check and score.",
        sub_agents=[ReconAgent(), inspector, ScoreAgent()],
    )


async def audit_via_graph(
    prospect: Mapping[str, Any],
    market: Any,
    definitions: list[Mapping[str, Any]],
    *,
    batch_id: str = "manual",
    persist: bool = True,
    on_event: Any = None,
) -> "AuditOutcome":
    """Run one audit through the ADK graph and persist it identically to the
    plain pipeline. Firestore cannot tell which path did the work."""
    from google.adk.runners import InMemoryRunner

    from app.pipeline import AuditOutcome, persist_audit

    holder: dict[str, Any] = {}

    def deliver(**kw: Any) -> None:
        holder.update(kw)

    runner = InMemoryRunner(build_audit_agent(), app_name="relay-audit")
    session = await runner.session_service.create_session(
        app_name="relay-audit",
        user_id="worker",
        state={
            "prospect": dict(prospect),
            "market": market,
            "definitions": definitions,
            "_deliver": deliver,
        },
    )

    async for event in runner.run_async(
        user_id="worker",
        session_id=session.id,
        new_message=genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=f"audit {prospect.get('business_name', '?')}")],
        ),
    ):
        if on_event is not None and event.content and event.content.parts:
            text = (event.content.parts[0].text or "").strip()
            if text:
                on_event(event.author, text)

    if "score" not in holder:
        raise RuntimeError("the audit graph finished without delivering a score")

    score = holder["score"]
    results = holder["results"]
    crawl_error = holder.get("crawl_error")

    audit_id = None
    if persist:
        audit_id = await persist_audit(
            prospect=prospect, batch_id=batch_id, score=score, results=results,
            definitions=definitions, crawl_error=crawl_error,
            pages_crawled=len(holder["site_facts"].pages),
            render=holder.get("render"),
        )

    return AuditOutcome(
        prospect=prospect, audit_id=audit_id, score=score, results=results,
        definitions=definitions, crawl_error=crawl_error,
    )
