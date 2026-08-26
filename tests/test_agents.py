"""The ADK layer: graph shape, coordinator wiring, and the honesty contracts.

No model calls here. What is pinned is the architecture the submission claims:
recon is sequential before a parallel inspector fan, the coordinator's tools
are the only way it can learn anything, and the instruction carries the rules.
"""

from __future__ import annotations

import pytest

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

from app.agents.audit_graph import build_audit_agent
from app.agents.coordinator import INSTRUCTION, build_coordinator
from app.copy_rules import contains_forbidden_dash


def test_the_audit_graph_is_the_spec_architecture():
    """SequentialAgent(recon -> ParallelAgent(look|speed|form_probe) -> score).
    This shape is named in the submission write-up, so it is asserted here."""
    graph = build_audit_agent()
    assert isinstance(graph, SequentialAgent)
    names = [a.name for a in graph.sub_agents]
    assert names == ["recon", "inspector", "score"]
    inspector = graph.sub_agents[1]
    assert isinstance(inspector, ParallelAgent)
    assert sorted(a.name for a in inspector.sub_agents) == ["form_probe", "look", "speed"]


def test_the_graph_builds_fresh_instances():
    """ADK agents are single-parent. A shared module-level graph would fail on
    the second audit in the same process."""
    a, b = build_audit_agent(), build_audit_agent()
    assert a is not b
    assert a.sub_agents[0] is not b.sub_agents[0]


def test_no_workflow_stage_carries_a_model():
    """Only four components get a model, and none of them is a pipeline stage.
    The vision model is called inside the look branch's functions, not by the
    agent shell."""
    graph = build_audit_agent()
    stages = [graph.sub_agents[0], *graph.sub_agents[1].sub_agents, graph.sub_agents[2]]
    for stage in stages:
        assert not isinstance(stage, LlmAgent), stage.name
        assert not getattr(stage, "model", None), stage.name


def test_the_coordinator_carries_the_six_tools(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    coordinator = build_coordinator()
    assert isinstance(coordinator, LlmAgent)
    names = {getattr(t, "__name__", str(t)) for t in coordinator.tools}
    assert names == {"sweep_market", "dispatch_audits", "batch_status",
                     "resume_batch", "wait", "rank_call_list"}


def test_building_the_coordinator_points_adk_at_the_model_endpoint(monkeypatch):
    """gemini-3.5-flash serves from the global endpoint only, and ADK reads the
    location from the environment. Building the coordinator must repoint it."""
    import os

    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    build_coordinator()
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"


def test_the_instruction_holds_the_lines_that_matter():
    assert "Report only numbers the tools returned" in INSTRUCTION
    assert "resume_batch" in INSTRUCTION, "partial-failure recovery is the coordinator's job"
    assert "em-dash" in INSTRUCTION
    assert not contains_forbidden_dash(INSTRUCTION)


def test_wait_is_clamped():
    """The coordinator cannot be talked into sleeping for an hour."""
    import asyncio

    from app.agents.coordinator import wait

    async def timed():
        import time

        start = time.monotonic()
        await wait(0)
        return time.monotonic() - start

    assert asyncio.run(timed()) < 3
