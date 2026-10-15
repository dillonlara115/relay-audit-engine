"""Search demand, AI answers, the review comparison and the short version."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Config
from app.report import extras
from app.report.data import forbidden_terms_in
from app.report.template import render_report
from app.copy_rules import contains_forbidden_dash
from app.tools import ai_answers, demand
from tests.test_console import client, sign_in  # noqa: F401
from tests.test_report import report
from tests.test_report_parts import _with_rivals, pool_store  # noqa: F401


@pytest.fixture()
def dfs(monkeypatch):
    for mod in (demand, ai_answers):
        monkeypatch.setattr(mod, "get_config", lambda: Config(dataforseo_login="l", dataforseo_password="p"))
    cache = {}
    for mod in (demand, ai_answers):
        monkeypatch.setattr(mod.store, "cache_get", lambda kind, req: cache.get(kind + json.dumps(req, sort_keys=True)))
        monkeypatch.setattr(mod.store, "cache_put", lambda kind, req, v: cache.update({kind + json.dumps(req, sort_keys=True): v}))
    return cache


# ── How many people are looking ───────────────────────────────────────────────


def test_the_town_rides_in_every_phrase():
    p = demand.phrases("Prescott", "AZ")
    assert "prescott roofing" in p and "roof repair prescott az" in p and len(p) == len(set(p))
    assert all("prescott" in x for x in p)


def test_only_phrases_google_counted_are_kept_biggest_first(dfs):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)[0])
        return httpx.Response(200, json={"tasks": [{"status_code": 20000, "result": [
            {"keyword": "prescott roofing", "search_volume": 390},
            {"keyword": "roofer prescott az", "search_volume": None},
            {"keyword": "roof repair prescott az", "search_volume": 20},
            {"keyword": "roofers prescott", "search_volume": 0}]}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    found = demand.look_up("Prescott", "AZ", client=http)
    again = demand.look_up("Prescott", "AZ", client=http)
    assert found == again == {"rows": [{"phrase": "prescott roofing", "monthly": 390},
                                       {"phrase": "roof repair prescott az", "monthly": 20}], "top": 390}
    assert len(sent) == 1 and sent[0]["location_code"] == 2840, "one paid lookup, then the cache"


def test_the_demand_section_never_adds_phrasings_up():
    snap = extras.demand_snapshot({"rows": [{"phrase": "prescott roofing", "monthly": 390},
                                            {"phrase": "roof repair prescott az", "monthly": 20}], "top": 390}, town="Prescott")
    page = render_report(report(demand=snap))
    assert "at least 390 searches a month for a roofer in Prescott" in page and "410" not in page
    assert "390 a month" in page and forbidden_terms_in(page) == []


# ── What AI assistants say ────────────────────────────────────────────────────

GPT = ("**I’d start with Heritage Roofing, Legacy Roofing, and Bradshaw Mountain Roofing.** | **Heritage Roofing** | "
       "lists **4.9/5 from 192 reviews**, **A+ and accredited** | **Legacy Roofing LLC** | **Bradshaw Mountain Roofing**")
PPX = "- **Legacy Roofing** — Prescott. - **Sun Spirit Roofing** — newer. **Before hiring**, get estimates."


def test_names_are_read_from_bold_text_once_each():
    assert ai_answers.named_businesses(GPT) == ["Heritage Roofing", "Legacy Roofing LLC", "Bradshaw Mountain Roofing"]
    assert ai_answers.named_businesses(PPX) == ["Legacy Roofing", "Sun Spirit Roofing"]


def test_they_are_found_by_name_without_the_llc_or_by_website():
    assert ai_answers.mentions(PPX, name="Sun Spirit Roofing LLC", domain=None)
    assert ai_answers.mentions("see sunspiritroofingllc.com", name="X", domain="www.sunspiritroofingllc.com")
    assert not ai_answers.mentions(GPT, name="Sun Spirit Roofing LLC", domain="sunspiritroofingllc.com")


def test_both_assistants_asked_and_read(dfs):
    def handler(request):
        text = GPT if "chat_gpt" in request.url.path else PPX
        return httpx.Response(200, json={"tasks": [{"status_code": 20000, "result": [
            {"items": [{"type": "message", "sections": [{"text": text}]}]}]}]})

    found = ai_answers.ask("Prescott", "AZ", name="Sun Spirit Roofing LLC", domain="sunspiritroofingllc.com",
                           client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert found["question"] == "Who are the best roofing companies in Prescott, AZ?"
    gpt, ppx = found["answers"]
    assert (gpt["assistant"], gpt["named_you"], len(gpt["named"])) == ("ChatGPT", False, 3)
    assert (ppx["assistant"], ppx["named_you"], ppx["named"]) == ("Perplexity", True, ["Legacy Roofing"])


def test_an_assistant_that_fails_is_left_out(dfs):
    def handler(request):
        if "chat_gpt" in request.url.path:
            return httpx.Response(200, json={"tasks": [{"status_code": 40501, "status_message": "bad"}]})
        return httpx.Response(200, json={"tasks": [{"status_code": 20000, "result": [{"items": [{"text": PPX}]}]}]})

    found = ai_answers.ask("Prescott", "AZ", name="Apex", domain=None, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [a["assistant"] for a in found["answers"]] == ["Perplexity"]


def test_the_ai_section_names_others_only_when_ticked():
    found = {"question": "Who are the best roofing companies in Prescott, AZ?", "answers": [
        {"assistant": "ChatGPT", "named_you": False, "named": ["Heritage Roofing", "Legacy Roofing LLC"]},
        {"assistant": "Perplexity", "named_you": True, "named": ["Legacy Roofing"]}]}
    page = render_report(report(ai=extras.ai_snapshot(found)))
    assert "<b>ChatGPT</b> didn't name you. It named 2 other roofers." in page
    assert "<b>Perplexity</b> named you." in page and "Heritage" not in page
    named = render_report(report(ai=extras.ai_snapshot(found, with_names=True)))
    assert "It named 2 other roofers: Heritage Roofing, Legacy Roofing LLC." in named
    assert forbidden_terms_in(named) == [] and not contains_forbidden_dash(named)


# ── How your reviews compare ──────────────────────────────────────────────────


def test_reviews_compared_with_the_roofers_who_show_up_most():
    prospect = {"review_count": 26, "rating": 5.0, "latest_review_at": datetime.now(timezone.utc) - timedelta(days=3)}
    snap = extras.reviews_snapshot(prospect, _with_rivals())
    assert snap["you"] == {"reviews": 26, "newest_days": 3, "rating": 5.0}
    assert [o["name"] for o in snap["others"]] == ["", "", ""], "no names unless ticked; one with no review count is left out"
    page = render_report(report(reviews=snap))
    assert "You have 26 reviews. The roofers who show up most often in Google Maps near you have" in page
    assert "Your newest review is 3 days old." in page and page.count('class="bar') == 4
    assert "Another roofer near you" in page and forbidden_terms_in(page) == []
    named = extras.reviews_snapshot(prospect, _with_rivals(), with_names=True)
    assert [o["name"] for o in named["others"]] == ["Arrow Roofing Inc.", "Heritage Roofing, Prescott", "Yavapai Roofing"]


def test_no_comparison_without_both_sides():
    assert extras.reviews_snapshot({"review_count": None}, _with_rivals()) is None
    assert extras.reviews_snapshot({"review_count": 26}, {"status": "done", "competitors": []}) is None


# ── The short version ─────────────────────────────────────────────────────────


def test_the_short_version_repeats_what_the_report_shows():
    reach = {"answered": 19, "top3": 0, "pins": []}
    page = render_report(report(reach=reach,
                                demand={"town": "Prescott", "top": 390, "rows": [{"phrase": "prescott roofing", "monthly": 390}]},
                                ai={"question": "q", "answers": [{"assistant": "ChatGPT", "named_you": False, "others": 3, "names": []},
                                                                 {"assistant": "Perplexity", "named_you": False, "others": 5, "names": []}]}))
    short = page.split("<h2>The short version</h2>")[1].split("</ul>")[0]
    assert "not in the top 3 at any of the 19 spots" in short
    assert "at least 390 searches a month for a roofer in Prescott" in short
    assert "The biggest thing costing you jobs:" in short and short.count("<li>") == 3
    assert page.index("The short version") < page.index("What we did")


def test_no_short_version_when_there_is_nothing_but_findings():
    assert "The short version" not in render_report(report())


# ── Saving ────────────────────────────────────────────────────────────────────


def test_saving_runs_only_the_lookups_ticked(client, pool_store, monkeypatch):
    calls = []
    monkeypatch.setattr(demand, "look_up", lambda city, state: calls.append("demand") or
                        {"rows": [{"phrase": "pueblo roofing", "monthly": 260}], "top": 260})
    monkeypatch.setattr(ai_answers, "ask", lambda *a, **k: calls.append("ai") or {"question": "q", "answers": []})
    csrf = sign_in(client)
    client.post("/console/audits/a1/report-sections", data={"csrf": csrf, "demand": "1"}, follow_redirects=False)
    saved = pool_store["saved"][-1]["report_extras"]
    assert calls == ["demand"] and saved["demand"]["top"] == 260 and saved["ai"] is None
    assert saved["demand"]["town"] == "Pueblo"


def test_a_failed_lookup_says_so(client, pool_store, monkeypatch):
    def down(*a, **k):
        raise ai_answers.AnswersUnavailable("Neither assistant answered.")
    monkeypatch.setattr(ai_answers, "ask", down)
    csrf = sign_in(client)
    r = client.post("/console/audits/a1/report-sections", data={"csrf": csrf, "ai": "1"}, follow_redirects=False)
    assert "notice=sections_partial" in r.headers["location"]
