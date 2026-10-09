"""Hunter: addresses for a prospect's domain, looked up only when asked."""

from __future__ import annotations

import httpx
import pytest

from app.config import Config
from app.console import views
from app.store import firestore as real_store
from app.tools import hunter
from app.tools.verify_email import INVALID, RISKY, VALID, Verdict
from tests.test_console import _list, _prospect_page, _row, client, sign_in  # noqa: F401

SEARCH = {"data": {"domain": "patriotroofco.com", "emails": [
    {"value": "info@patriotroofco.com", "type": "generic", "confidence": 95,
     "sources": [{}, {}], "verification": {"status": "valid"}},
    {"value": "Connor@PatriotRoofCo.com", "type": "personal", "confidence": 80,
     "first_name": "Connor", "last_name": "Ray", "position": "Owner",
     "sources": [{}], "verification": {"status": "accept_all"}},
    {"value": "not an email", "type": "personal", "confidence": 99},
]}}


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setattr(hunter, "get_config", lambda: Config(hunter_api_key="k-test"))


def _client(status=200, body=None):
    def handler(request):
        assert request.url.params["api_key"] == "k-test"
        return httpx.Response(status, json=body if body is not None else SEARCH)
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── The client ────────────────────────────────────────────────────────────────


def test_a_domain_search_returns_named_people_first_and_drops_junk(keyed):
    found = hunter.domain_search("patriotroofco.com", client=_client())
    assert [f.email for f in found] == ["connor@patriotroofco.com", "info@patriotroofco.com"]
    owner = found[0]
    assert owner.personal and owner.name == "Connor Ray" and owner.position == "Owner"
    assert owner.hunter_status == "accept_all" and owner.sources == 1


@pytest.mark.parametrize("status, words", [(401, "refused"), (429, "out of searches"),
                                           (402, "out of searches"), (500, "500")])
def test_hunter_errors_say_plainly_what_went_wrong(keyed, status, words):
    with pytest.raises(hunter.HunterUnavailable, match=words):
        hunter.domain_search("x.com", client=_client(status, {"errors": [{"details": "boom"}]}))


def test_no_key_means_no_search(monkeypatch):
    monkeypatch.setattr(hunter, "get_config", lambda: Config(hunter_api_key=""))
    with pytest.raises(hunter.HunterUnavailable, match="HUNTER_API_KEY"):
        hunter.domain_search("x.com", client=_client())


def test_searches_left_reads_the_account(keyed):
    body = {"data": {"requests": {"searches": {"used": 7, "available": 25}}}}
    assert hunter.searches_left(client=_client(body=body)) == 18


# ── Verification: Hunter can only lower our verdict ───────────────────────────


@pytest.mark.parametrize("ours, theirs, expected", [
    (VALID, "valid", VALID), (VALID, "accept_all", RISKY), (VALID, "invalid", INVALID),
    (RISKY, "valid", RISKY), (INVALID, "valid", INVALID),
])
def test_hunters_verdict_never_raises_ours(monkeypatch, ours, theirs, expected):
    monkeypatch.setattr(hunter, "verify", lambda e: Verdict(e, ours, "checked"))
    row = hunter.to_contact(hunter.Found("dave@apex.com", True, hunter_status=theirs, sources=3),
                            domain="apex.com")
    assert row["status"] == expected and row["source"] == "hunter" and row["own_domain"]
    assert "Seen in 3 public sources." in row["reason"]


# ── Which address outreach writes to ──────────────────────────────────────────


def test_a_typed_address_beats_the_site_which_beats_hunter():
    site = [{"email": "info@apex.com", "status": "risky"}]
    found = [{"email": "dave@apex.com", "status": "valid"}]
    assert real_store.owner_from([{"email": "me@apex.com"}], site, found) == "me@apex.com"
    assert real_store.owner_from([], site, found) == "info@apex.com"
    assert real_store.owner_from([], [], found) == "dave@apex.com"
    assert real_store.owner_from([], [], [{"email": "x@apex.com", "status": "invalid"}]) is None


# ── The route ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def hunt(client, monkeypatch):
    import app.console.routes as routes

    state = {"searched": [], "stored": {}, "used": 0, "cap": 25, "key": "k"}
    prospects = {
        "p1": {"domain": "apex.com"},
        "p2": {"domain": "peak.com", "owner_email": "info@peak.com"},
        "p3": {},
        "p4": {"domain": "ridge.com"},
    }
    monkeypatch.setattr(routes, "get_config",
                        lambda: Config(hunter_api_key=state["key"], hunter_monthly_cap=state["cap"]))
    monkeypatch.setattr(routes.store, "audits_for_batch",
                        lambda bid: iter([{"audit_id": f"a{i}", "prospect_id": f"p{i}"} for i in range(1, 5)]))
    monkeypatch.setattr(routes.store, "prospects_by_id", lambda ids: {i: prospects[i] for i in ids})
    monkeypatch.setattr(routes.store, "load_suppressions", lambda: {})
    monkeypatch.setattr(routes.store, "suppression_hit", lambda rules, **kw: None)
    monkeypatch.setattr(routes.store, "hunter_searches", lambda month: state["used"])
    monkeypatch.setattr(routes.store, "bump_hunter_searches", lambda month, n=1: state.__setitem__("used", state["used"] + n))
    monkeypatch.setattr(routes.store, "set_hunter_contacts",
                        lambda pid, rows: state["stored"].__setitem__(pid, rows) or (rows[0]["email"] if rows else None))
    monkeypatch.setattr(hunter, "domain_search",
                        lambda domain, **kw: state["searched"].append(domain) or [hunter.Found(f"dave@{domain}", True)])
    monkeypatch.setattr(hunter, "to_contact", lambda f, domain: {"email": f.email, "status": "valid"})
    monkeypatch.setattr(hunter, "searches_left", lambda **kw: 17)
    return state


def _hunt(client, csrf, **data):
    return client.post("/console/contacts/hunter", data={"csrf": csrf, "batch_id": "b1", **data},
                       follow_redirects=False, headers={"referer": "http://testserver/console/batches/b1"})


def test_bulk_searches_only_prospects_with_no_address_and_a_website(client, hunt):
    csrf = sign_in(client)
    r = _hunt(client, csrf, audit_ids="a1,a2,a3,a4")
    assert "notice=hunter_done" in r.headers["location"]
    assert sorted(hunt["searched"]) == ["apex.com", "ridge.com"], "p2 has an address, p3 has no website"
    assert hunt["used"] == 2 and set(hunt["stored"]) == {"p1", "p4"}
    assert "17+Hunter+searches+left" in r.headers["location"]


def test_one_prospects_page_searches_even_when_an_address_exists(client, hunt):
    csrf = sign_in(client)
    _hunt(client, csrf, prospect_id="p2")
    assert hunt["searched"] == ["peak.com"]


def test_the_monthly_cap_stops_the_spend(client, hunt):
    hunt["used"], hunt["cap"] = 24, 25
    csrf = sign_in(client)
    r = _hunt(client, csrf, audit_ids="a1,a4")
    assert len(hunt["searched"]) == 1 and "Stopped+at+the+monthly+cap" in r.headers["location"]
    hunt["used"] = 25
    r = _hunt(client, csrf, audit_ids="a1,a4")
    assert "notice=hunter_failed" in r.headers["location"] and len(hunt["searched"]) == 1


def test_without_a_key_nothing_is_searched(client, hunt):
    hunt["key"] = ""
    csrf = sign_in(client)
    r = _hunt(client, csrf, audit_ids="a1")
    assert "notice=hunter_failed" in r.headers["location"] and hunt["searched"] == []


# ── Where it shows ────────────────────────────────────────────────────────────


def test_the_call_list_can_look_up_the_ticked_rows():
    page = _list([_row(1)])
    assert 'action="/console/contacts/hunter"' in page and 'id="hunter-ids"' in page
    assert "hunterIds.value = ids.join(',')" in page


def test_an_address_from_hunter_says_so_on_the_call_list():
    cell = views.contact_cell([{"email": "dave@apex.com", "status": "valid", "source": "hunter"}])
    assert "dave@apex.com" in cell and "via Hunter" in cell


def test_a_prospect_with_no_address_is_offered_a_hunter_search():
    page = _prospect_page(findings={"status": "approved", "selected": [1, 2, 3],
                                    "findings": [{"ordinal": i} for i in range(1, 7)]},
                          audit={"report_slug": "abcdefghijklmnop"})
    assert "Find an email with Hunter" in page and 'name="prospect_id" value="p1"' in page
    searched = _prospect_page(findings={"status": "approved", "selected": [1, 2, 3],
                                        "findings": [{"ordinal": i} for i in range(1, 7)]},
                              audit={"report_slug": "abcdefghijklmnop"},
                              prospect={"hunter_checked_at": "2026-10-09"})
    assert "Search Hunter again" in searched and "found no address worth writing to" in searched
