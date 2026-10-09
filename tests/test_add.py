"""Add a roofer: one business found on Google Maps by name and town, saved as
a prospect and audited outside any sweep."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import jobs
from app.config import Config
from app.tools import places
from tests.test_console import client, queued, sign_in  # noqa: F401


def _place(pid, name, **over):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": "12 Main St, Colorado Springs, CO 80903",
            "addressComponents": [{"types": ["locality"], "longText": "Colorado Springs"},
                                  {"types": ["administrative_area_level_1"], "shortText": "CO"}],
            "location": {"latitude": 38.83, "longitude": -104.82}, "rating": 4.9, "userRatingCount": 87,
            "websiteUri": "https://www.greatdaneroofing.com/", "nationalPhoneNumber": "(719) 555-0101",
            "businessStatus": "OPERATIONAL", "googleMapsUri": "https://maps.google.com/?cid=9", **over}


# ── The lookup ────────────────────────────────────────────────────────────────


def test_one_search_first_page_only_through_the_cache(monkeypatch):
    sent, cached = [], {}
    monkeypatch.setattr(places, "get_config", lambda: Config(places_api_key="k"))
    monkeypatch.setattr(places.store, "cache_get", lambda kind, req: cached.get(json.dumps(req)))
    monkeypatch.setattr(places.store, "cache_put", lambda kind, req, payload: cached.update({json.dumps(req): payload}))

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"places": [_place(f"p{i}", f"Roofer {i}") for i in range(8)],
                                         "nextPageToken": "more"})

    real = httpx.AsyncClient
    monkeypatch.setattr(places.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    first = asyncio.run(places.find_business("  Great Dane   Roofing  Colorado Springs "))
    again = asyncio.run(places.find_business("Great Dane Roofing Colorado Springs"))
    assert len(first) == places.FIND_LIMIT and [r.place_id for r in again] == [r.place_id for r in first]
    assert sent == [{"textQuery": "Great Dane Roofing Colorado Springs", "pageSize": 20}], "one call, then the cache"


def test_a_blank_lookup_asks_nothing(monkeypatch):
    monkeypatch.setattr(places, "get_config", lambda: Config(places_api_key="k"))
    assert asyncio.run(places.find_business("   ")) == []


def test_a_sweep_and_a_single_add_write_the_same_prospect_fields():
    record = places.flatten_place(_place("p1", "Great Dane Roofing"))
    fields = places.prospect_fields(record, market_id="colorado-springs", batch_id="manual")
    assert fields["domain"] == "greatdaneroofing.com" and fields["lat"] == 38.83
    assert fields["city"] == "Colorado Springs" and fields["latest_batch_id"] == "manual"


# ── The pages ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def lookup(monkeypatch):
    """Places answers with three listings: new, already a prospect, suppressed."""
    import app.console.routes as routes

    env = {"prospects": {"p2": {"latest_audit_id": "a2", "market_id": "pueblo"}},
           "upserts": [], "calls": 0}
    records = [places.flatten_place(_place("p1", "Great Dane Roofing")),
               places.flatten_place(_place("p2", "Great Dane Exteriors")),
               places.flatten_place(_place("p3", "Dane Bros Roofing", websiteUri="https://danebros.com/"))]

    async def find(q, **kw):
        env["calls"] += 1
        return records

    monkeypatch.setattr(places, "find_business", find)
    monkeypatch.setattr(routes.store, "get_prospect", lambda pid: env["prospects"].get(pid))
    monkeypatch.setattr(routes.store, "load_suppressions",
                        lambda: {"place_id": set(), "domain": {"danebros.com"}, "phone": set(), "email": set()})
    monkeypatch.setattr(routes.store, "upsert_prospect", lambda pid, f: env["upserts"].append((pid, f)))
    return env


def test_the_page_lists_each_match_with_what_we_already_hold(client, lookup):
    sign_in(client)
    page = client.get("/console/add?q=great+dane").text
    assert "Great Dane Roofing" in page and "4.9 stars, 87 reviews" in page and "greatdaneroofing.com" in page
    assert page.count('action="/console/add"') == 1 + 1, "the search form, and one Add for the new listing"
    assert 'href="/console/audits/a2"' in page and "Already a prospect" in page
    assert "Do not contact" in page


def test_the_page_without_a_search_does_not_look_anything_up(client, lookup):
    sign_in(client)
    page = client.get("/console/add").text
    assert lookup["calls"] == 0 and 'name="q"' in page and "Pick the right listing" not in page


def test_no_match_says_so(client, lookup, monkeypatch):
    async def none(q, **kw):
        return []
    monkeypatch.setattr(places, "find_business", none)
    sign_in(client)
    assert "No Google listing matched" in client.get("/console/add?q=nobody").text


def test_a_failed_lookup_is_a_line_not_a_500(client, lookup, monkeypatch):
    async def boom(q, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(places, "find_business", boom)
    sign_in(client)
    r = client.get("/console/add?q=x")
    assert r.status_code == 200 and "Google Maps did not answer" in r.text


def test_the_overview_and_nav_lead_here(client, lookup):
    sign_in(client)
    page = client.get("/console").text
    assert 'action="/console/add"' in page and 'href="/console/add"' in page


# ── Adding ────────────────────────────────────────────────────────────────────


def _add(client, csrf, pid, q="great dane"):
    return client.post("/console/add", data={"csrf": csrf, "q": q, "place_id": pid}, follow_redirects=False)


def test_adding_saves_the_listing_and_audits_it_back_to_its_page(client, queued, lookup):
    from app.store.firestore import audit_doc_id

    csrf = sign_in(client)
    r = _add(client, csrf, "p1")
    assert r.status_code == 303 and r.headers["location"] == "/console/jobs/job-1"
    pid, fields = lookup["upserts"][0]
    assert pid == "p1" and fields["source"] == "manual" and fields["market_id"] == "colorado-springs"
    assert fields["website_url"] == "https://www.greatdaneroofing.com/" and fields["latest_batch_id"] == "manual"
    kind, params, label = queued[0]
    assert kind == jobs.KIND_AUDIT and label == "Audit Great Dane Roofing"
    assert params == {"place_id": "p1", "batch_id": "manual",
                      "return_to": f"/console/audits/{audit_doc_id('p1', 'manual')}"}


def test_a_known_prospect_keeps_its_market(client, queued, lookup):
    csrf = sign_in(client)
    _add(client, csrf, "p2")
    assert lookup["upserts"][0][1]["market_id"] == "pueblo"


def test_a_suppressed_business_is_never_added(client, queued, lookup):
    csrf = sign_in(client)
    r = _add(client, csrf, "p3")
    url = httpx.URL(r.headers["location"])
    assert url.params["notice"] == "not_queued" and "do-not-contact" in url.params["detail"]
    assert not lookup["upserts"] and not queued


def test_a_place_id_not_in_the_results_is_refused(client, queued, lookup):
    """Nothing about the business comes from the browser but which one to pick."""
    csrf = sign_in(client)
    r = _add(client, csrf, "forged")
    assert "notice=not_queued" in r.headers["location"] and not lookup["upserts"] and not queued


def test_adding_needs_a_fresh_form(client, queued, lookup):
    sign_in(client)
    assert _add(client, "stale", "p1").status_code == 403 and not lookup["upserts"]


def test_the_manual_call_list_is_labelled(client, monkeypatch):
    import app.console.routes as routes

    monkeypatch.setattr(routes, "_assemble_batch", lambda bid: ([], {}, []))
    monkeypatch.setattr(routes, "_excluded_for_batch", lambda bid: [])
    sign_in(client)
    assert "Added by hand" in client.get("/console/batches/manual").text
