"""Local reach: a grid of Maps searches around the business, the job that
runs it, and the console section that shows it."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app import job_runner, jobs
from app.config import Config
from app.tools import reach
from tests.test_console import _prospect_page, client, queued, sign_in  # noqa: F401


def _item(rank, title, place_id, rating=4.8, votes=120, domain=""):
    return {"type": "maps_search", "rank_group": rank, "rank_absolute": rank, "title": title,
            "place_id": place_id, "domain": domain, "rating": {"value": rating, "votes_count": votes}}


def _body(items, cost=0.002):
    return {"cost": cost, "tasks": [{"status_code": 20000, "cost": cost, "result": [{"items": items}]}]}


# ── The grid ──────────────────────────────────────────────────────────────────


def test_the_grid_is_square_centred_and_north_up():
    pts = reach.grid(38.8, -104.8, size=5, radius_miles=5)
    assert len(pts) == 25
    middle = pts[12]
    assert (middle.row, middle.col, middle.lat, middle.lng) == (2, 2, 38.8, -104.8)
    assert pts[0].lat > middle.lat and pts[0].lng < middle.lng, "row 0, col 0 is the north-west corner"
    assert round((pts[0].lat - middle.lat) * 69.0, 2) == 5.0, "the edge is radius miles out"


# ── One point ─────────────────────────────────────────────────────────────────


def test_they_are_found_by_place_id_not_by_a_similar_name():
    body = _body([_item(1, "Apex Roofing Co", "other"), _item(2, "Rival", "r1"), _item(4, "Apex", "p1")])
    rank, top, cost = reach.read_point(body, place_id="p1", domain="", name="apex roofing co x")
    assert rank == 4 and cost == 0.002
    assert [t["title"] for t in top] == ["Apex Roofing Co", "Rival"] and not any(t["mine"] for t in top)


def test_a_domain_or_exact_name_match_stands_in_for_a_missing_place_id():
    body = _body([_item(3, "Apex", "", domain="www.apex.com")])
    assert reach.read_point(body, place_id="p1", domain="apex.com", name="")[0] == 3
    body = _body([_item(6, "Apex Roofing", "")])
    assert reach.read_point(body, place_id="p1", domain="", name="apex roofing")[0] == 6


def test_not_in_the_list_is_none():
    assert reach.read_point(_body([_item(1, "Rival", "r1")]), place_id="p1", domain="", name="")[0] is None


def test_a_refused_task_raises():
    body = {"tasks": [{"status_code": 40200, "status_message": "Payment Required."}]}
    with pytest.raises(reach.ReachUnavailable, match="Payment Required"):
        reach.read_point(body, place_id="p1", domain="", name="")


# ── A run ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def dfs(monkeypatch):
    monkeypatch.setattr(reach, "get_config", lambda: Config(dataforseo_login="l", dataforseo_password="p"))


def _run(handler, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            args = {"place_id": "p1", "domain": "apex.com", "name": "Apex", "lat": 38.8, "lng": -104.8,
                    "keyword": "roofer", "size": 5, "radius_miles": 5, **kw}
            return await reach.run(client=http, **args)
    return asyncio.run(go())


def test_every_point_is_searched_from_its_own_coordinates(dfs):
    sent = []

    def handler(request):
        task = json.loads(request.content)[0]
        sent.append(task)
        lat = float(task["location_coordinate"].split(",")[0])
        # In the top three north of the business, sixth at its latitude, gone to the south.
        if lat > 38.81:
            return httpx.Response(200, json=_body([_item(1, "Apex", "p1"), _item(2, "Rival", "r1", votes=300)]))
        if lat > 38.79:
            return httpx.Response(200, json=_body([_item(1, "Rival", "r1", votes=300), _item(6, "Apex", "p1")]))
        return httpx.Response(200, json=_body([_item(1, "Other", "o1", votes=10), _item(2, "Rival", "r1", votes=300)]))

    out = _run(handler)
    assert len(sent) == 25 and all(t["keyword"] == "roofer" and t["location_coordinate"].endswith(",13z") for t in sent)
    assert len({t["location_coordinate"] for t in sent}) == 25
    assert (out["answered"], out["top3"], out["found"], out["average_rank"]) == (25, 10, 15, 2.7)
    assert out["cost"] == 0.05
    assert out["competitors"][0] == {"title": "Rival", "rating": 4.8, "reviews": 300, "spots": 25}
    assert [c["title"] for c in out["competitors"]] == ["Rival", "Other"], "the business itself is never a competitor"


def test_a_point_that_fails_is_marked_and_the_rest_still_count(dfs):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=_body([_item(2, "Apex", "p1")]))

    out = _run(handler)
    assert out["answered"] == 24 and out["top3"] == 24
    assert sum(1 for p in out["points"] if p["error"]) == 1


def test_when_nothing_comes_back_the_run_fails(dfs):
    with pytest.raises(reach.ReachUnavailable, match="500"):
        _run(lambda r: httpx.Response(500))


def test_without_credentials_nothing_is_searched(monkeypatch):
    monkeypatch.setattr(reach, "get_config", lambda: Config(dataforseo_login="", dataforseo_password=""))
    with pytest.raises(reach.ReachUnavailable, match="credentials"):
        _run(lambda r: pytest.fail("no search without credentials"))


# ── The job ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def reach_env(monkeypatch):
    env = {"updates": [], "runs": [], "audit": {"prospect_id": "p1"}}
    monkeypatch.setattr(job_runner.jobs, "log", lambda job_id, line: None)
    monkeypatch.setattr(job_runner.store, "get_audit", lambda aid: env["audit"])
    monkeypatch.setattr(job_runner.store, "get_prospect",
                        lambda pid: {"lat": 38.8, "lng": -104.8, "domain": "apex.com", "business_name": "Apex"})
    monkeypatch.setattr(job_runner.store, "update_audit", lambda aid, f: env["updates"].append(f))
    monkeypatch.setattr("app.config.get_config", lambda: Config(dataforseo_login="l", dataforseo_password="p"))

    async def fake_run(**kw):
        env["runs"].append(kw)
        spots = [reach.Spot(0, 0, 1, 1, rank=2)]
        return reach.summarize(spots, keyword=kw["keyword"], size=kw["size"], radius_miles=kw["radius_miles"], cost=0.1)

    monkeypatch.setattr(reach, "run", fake_run)
    return env


def test_the_job_searches_with_the_chosen_settings_and_saves_the_result(reach_env):
    asyncio.run(job_runner.run_reach_job("j1", {"audit_id": "a1", "keyword": "roof repair", "size": "7", "radius": "10"}))
    run = reach_env["runs"][0]
    assert (run["keyword"], run["size"], run["radius_miles"], run["place_id"]) == ("roof repair", 7, 10, "p1")
    saved = reach_env["updates"][-1]["local_reach"]
    assert saved["status"] == "done" and saved["job_id"] == "j1" and saved["top3"] == 1


def test_unknown_settings_fall_back_and_the_point_cap_shrinks_the_grid(reach_env, monkeypatch):
    monkeypatch.setattr("app.config.get_config", lambda: Config(reach_max_points=25))
    asyncio.run(job_runner.run_reach_job("j1", {"audit_id": "a1", "keyword": "drop table", "size": "7", "radius": "x"}))
    run = reach_env["runs"][0]
    assert (run["keyword"], run["size"], run["radius_miles"]) == ("roofer", 5, 5)


def test_a_redelivered_job_does_not_search_again(reach_env):
    reach_env["audit"] = {"prospect_id": "p1", "local_reach": {"job_id": "j1", "status": "done"}}
    asyncio.run(job_runner.run_reach_job("j1", {"audit_id": "a1"}))
    assert reach_env["runs"] == [] and reach_env["updates"] == []


def test_a_refused_run_is_recorded_not_raised(reach_env, monkeypatch):
    async def refuse(**kw):
        raise reach.ReachUnavailable("DataForSEO: Payment Required.")
    monkeypatch.setattr(reach, "run", refuse)
    asyncio.run(job_runner.run_reach_job("j1", {"audit_id": "a1"}))
    saved = reach_env["updates"][-1]["local_reach"]
    assert saved["status"] == "failed" and saved["error"] == "DataForSEO: Payment Required." and saved["job_id"] == "j1"


def test_the_reach_job_is_registered():
    assert job_runner.RUNNERS[jobs.KIND_REACH] is job_runner.run_reach_job


# ── Route ─────────────────────────────────────────────────────────────────────


def test_run_reach_queues_a_job_that_comes_back_to_the_section(client, queued, monkeypatch):
    import app.console.routes as routes

    monkeypatch.setattr(routes.store, "get_audit", lambda aid: {"prospect_id": "p1"})
    monkeypatch.setattr(routes.store, "get_prospect", lambda pid: {"business_name": "Apex"})
    csrf = sign_in(client)
    client.post("/console/audits/a1/reach", data={"csrf": csrf, "keyword": "roofer", "size": "5", "radius": "3"},
                follow_redirects=False)
    kind, params, label = queued[0]
    assert kind == jobs.KIND_REACH and label == "Local reach for Apex"
    assert params == {"audit_id": "a1", "keyword": "roofer", "size": "5", "radius": "3",
                      "return_to": "/console/audits/a1#reach"}


# ── The section ───────────────────────────────────────────────────────────────


def _page(audit=None, prospect=None):
    return _prospect_page(audit={"report_slug": "x", **(audit or {})},
                          prospect={"website_url": "https://apex.com/", "lat": 38.8, "lng": -104.8, **(prospect or {})})


def _record():
    spots = [reach.Spot(r, c, 38.8, -104.8, rank=(1 if r == 0 else 12 if r == 1 else None),
                        top=[{"title": "Rival", "place_id": "r1", "rating": 4.9, "reviews": 210, "mine": False}])
             for r in range(3) for c in range(3)]
    spots[4].error = "DataForSEO returned 500"
    return {**reach.summarize(spots, keyword="roof repair", size=3, radius_miles=3, cost=0.018),
            "status": "done", "finished_at": datetime(2026, 10, 9, tzinfo=timezone.utc)}


def test_the_section_shows_the_grid_the_numbers_and_who_holds_the_top_three():
    page = _page({"local_reach": _record()})
    assert 'id="reach"' in page and "Last run Oct 09, 2026" in page
    assert page.count('class="rcell good"') == 3 and page.count('class="rcell poor"') == 2
    assert 'class="rcell none center"' in page, "the failed middle spot is still ringed as the business"
    assert "3 of 8 spots" in page and "Rival" in page and "210" in page and "$0.02" in page
    assert "google.com/maps/search/roof%20repair/@38.8,-104.8,13z" in page
    assert 'action="/console/audits/a1/reach"' in page and '<option value="roof repair" selected>' in page


def test_before_a_run_the_section_says_what_to_do():
    page = _page()
    assert "Not run yet." in page and 'action="/console/audits/a1/reach"' in page


def test_without_a_map_location_there_is_no_run_button():
    page = _page(prospect={"lat": None, "lng": None})
    assert "No map location on record" in page and 'action="/console/audits/a1/reach"' not in page


def test_a_failed_run_says_why():
    page = _page({"local_reach": {"status": "failed", "error": "DataForSEO: Payment Required."}})
    assert "The last run did not finish: DataForSEO: Payment Required." in page
