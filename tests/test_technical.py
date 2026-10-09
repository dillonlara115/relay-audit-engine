"""The console's Technical section: Lighthouse on every audit, and a
DataForSEO crawl when a person asks for one."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app import job_runner, jobs
from app.config import Config
from app.console import views
from app.tools import onpage, pagespeed
from tests.test_console import _prospect_page, client, queued, sign_in  # noqa: F401

LH = {
    "lighthouseResult": {
        "finalUrl": "https://apex.com/",
        "categories": {
            "performance": {"score": 0.34, "auditRefs": [
                {"id": "largest-contentful-paint", "weight": 25, "group": "metrics"},
                {"id": "unused-javascript", "weight": 0},
                {"id": "uses-responsive-images", "weight": 0},
                {"id": "screenshot-thumbnails", "weight": 0},
            ]},
            "accessibility": {"score": 0.81, "auditRefs": [
                {"id": "image-alt", "weight": 10}, {"id": "color-contrast", "weight": 7},
                {"id": "html-has-lang", "weight": 7}]},
            "best-practices": {"score": 0.92, "auditRefs": []},
            "seo": {"score": 0.85, "auditRefs": [{"id": "meta-description", "weight": 1}]},
        },
        "audits": {
            "largest-contentful-paint": {"numericValue": 6840, "score": 0.1, "scoreDisplayMode": "numeric"},
            "first-contentful-paint": {"numericValue": 3100},
            "speed-index": {"numericValue": 7900},
            "total-blocking-time": {"numericValue": 1180},
            "cumulative-layout-shift": {"numericValue": 0.214},
            "unused-javascript": {"title": "Reduce unused JavaScript", "score": 0.3,
                                  "scoreDisplayMode": "metricSavings", "displayValue": "Savings of 412 KiB"},
            "uses-responsive-images": {"title": "Properly size images", "score": 0.95,
                                       "scoreDisplayMode": "metricSavings"},
            "screenshot-thumbnails": {"title": "Thumbnails", "scoreDisplayMode": "informative"},
            "image-alt": {"title": "Image elements do not have `[alt]` attributes", "score": 0,
                          "scoreDisplayMode": "binary"},
            "color-contrast": {"title": "Low contrast", "score": 0, "scoreDisplayMode": "binary"},
            "html-has-lang": {"title": "Has lang", "score": 1, "scoreDisplayMode": "binary"},
            "meta-description": {"title": "No meta description", "score": 0, "scoreDisplayMode": "binary"},
            "final-screenshot": {"details": {"data": "data:image/jpeg;base64,/9j/AAAA"}},
        },
    },
}


# ── PageSpeed: every category, in one call ────────────────────────────────────


def test_flatten_keeps_every_category_the_failing_items_and_googles_screenshot():
    r = pagespeed.flatten(LH, "https://apex.com/", "mobile")
    assert (r.performance_score, r.accessibility_score, r.best_practices_score, r.seo_score) == (34, 81, 92, 85)
    assert r.speed_index_ms == 7900 and r.screenshot_b64 == "/9j/AAAA"
    titles = [i["title"] for i in r.issues]
    assert "Reduce unused JavaScript" in titles and "Image elements do not have [alt] attributes" in titles
    assert "Properly size images" not in titles, "a 0.95 is passing"
    assert "Thumbnails" not in titles and "Has lang" not in titles
    assert not any(i["title"].startswith("Largest") for i in r.issues), "metrics are shown as numbers, not issues"
    assert r.report_url == "https://pagespeed.web.dev/report?url=https%3A%2F%2Fapex.com%2F&form_factor=mobile"


def test_one_psi_call_asks_for_all_four_categories(monkeypatch):
    seen = {}

    def handler(request):
        seen["categories"] = request.url.params.get_list("category")
        return httpx.Response(200, json=LH)

    monkeypatch.setattr(pagespeed, "get_config", lambda: Config(pagespeed_api_key="k"))
    monkeypatch.setattr(pagespeed.store, "cache_get", lambda *a: None)
    monkeypatch.setattr(pagespeed.store, "cache_put", lambda *a: None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(pagespeed.analyze("https://apex.com/", client=client))
    assert seen["categories"] == ["PERFORMANCE", "ACCESSIBILITY", "BEST_PRACTICES", "SEO"]
    assert result.seo_score == 85


def test_a_cache_entry_from_before_every_category_is_a_miss(monkeypatch):
    calls = []
    monkeypatch.setattr(pagespeed, "get_config", lambda: Config(pagespeed_api_key="k"))
    monkeypatch.setattr(pagespeed.store, "cache_get", lambda *a: {"ok": True, "url": "u", "performance_score": 50})
    monkeypatch.setattr(pagespeed.store, "cache_put", lambda *a: None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: calls.append(1) or httpx.Response(200, json=LH)))
    asyncio.run(pagespeed.analyze("https://apex.com/", client=client))
    assert calls == [1]


def test_every_audit_saves_lighthouse_and_googles_screenshot(monkeypatch):
    from app import pipeline
    from app.store import evidence as evidence_store

    updates, uploads = [], []
    monkeypatch.setattr(pipeline.store, "update_audit", lambda aid, f: updates.append((aid, f)))
    monkeypatch.setattr(evidence_store, "upload", lambda *a, **kw: uploads.append((a, kw)))
    asyncio.run(pipeline.save_lighthouse("p1", "a1", pagespeed.flatten(LH, "https://apex.com/", "mobile")))
    record = updates[0][1]["lighthouse"]
    assert record["scores"] == {"performance": 34, "accessibility": 81, "best_practices": 92, "seo": 85}
    assert record["psi_url"].startswith("https://pagespeed.web.dev/report?url=")
    assert "screenshot_b64" not in str(record), "the image goes to storage, not the document"
    (pid, aid, name, data), kw = uploads[0]
    assert (pid, aid, name, kw["kind"], kw["content_type"]) == ("p1", "a1", "lighthouse.jpg", "lighthouse", "image/jpeg")


# ── DataForSEO On-Page ────────────────────────────────────────────────────────

SUMMARY = {"tasks": [{"status_code": 20000, "result": [{
    "crawl_progress": "finished", "crawl_status": {"pages_crawled": 46},
    "domain_info": {"cms": "WordPress", "checks": {"sitemap": True, "robots_txt": False},
                    "ssl_info": {"valid_certificate": True}},
    "page_metrics": {"onpage_score": 78.4, "broken_links": 7, "duplicate_title": 0,
                     "checks": {"no_title": 2, "no_h1_tag": 0, "no_image_alt": 31}}}]}]}


@pytest.fixture()
def dfs(monkeypatch):
    monkeypatch.setattr(onpage, "get_config", lambda: Config(dataforseo_login="l", dataforseo_password="p"))


def _http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_crawl_is_posted_with_the_page_cap(dfs):
    sent = {}

    def handler(request):
        import json
        sent.update(json.loads(request.content)[0])
        return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T1"}]})

    assert onpage.start("apex.com", max_pages=50, client=_http(handler)) == "T1"
    assert sent["target"] == "apex.com" and sent["max_crawl_pages"] == 50


def test_a_crawl_in_progress_reads_as_not_ready(dfs):
    body = {"tasks": [{"status_code": 20000, "result": [{"crawl_progress": "in_progress"}]}]}
    assert onpage.summary("T1", client=_http(lambda r: httpx.Response(200, json=body))) is None


def test_the_summary_becomes_plain_issues_and_site_checks(dfs):
    result = onpage.summary("T1", client=_http(lambda r: httpx.Response(200, json=SUMMARY)))
    d = onpage.distil(result)
    assert d["health"] == 78 and d["pages_crawled"] == 46 and d["cms"] == "WordPress"
    assert [(i["label"], i["count"]) for i in d["issues"]] == [
        ("Broken links", 7), ("Pages with no title", 2), ("Pages with images missing alt text", 31)]
    assert [s["ok"] for s in d["site"]] == [True, True, False]


def test_without_credentials_no_crawl_is_attempted(monkeypatch):
    monkeypatch.setattr(onpage, "get_config", lambda: Config(dataforseo_login="", dataforseo_password=""))
    with pytest.raises(onpage.OnPageUnavailable, match="credentials"):
        onpage.start("apex.com", max_pages=10, client=_http(lambda r: httpx.Response(500)))


# ── The job ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def tech_env(monkeypatch):
    from app import pipeline

    env = {"updates": [], "saved": [], "started": [], "summary": [None, SUMMARY["tasks"][0]["result"][0]]}
    monkeypatch.setattr(job_runner.jobs, "log", lambda job_id, line: None)
    monkeypatch.setattr(job_runner.store, "get_audit", lambda aid: {"prospect_id": "p1"})
    monkeypatch.setattr(job_runner.store, "get_prospect",
                        lambda pid: {"website_url": "https://apex.com/", "domain": "apex.com"})
    monkeypatch.setattr(job_runner.store, "update_audit", lambda aid, f: env["updates"].append(f))
    monkeypatch.setattr(job_runner, "TECHNICAL_POLL_EVERY", 0)

    async def fake_analyze(url, **kw):
        return pagespeed.flatten(LH, url, "mobile")

    async def fake_save(pid, aid, psi):
        env["saved"].append((pid, aid, psi.seo_score))

    monkeypatch.setattr(pagespeed, "analyze", fake_analyze)
    monkeypatch.setattr(pipeline, "save_lighthouse", fake_save)
    monkeypatch.setattr(onpage, "start", lambda domain, **kw: env["started"].append(domain) or "T1")
    monkeypatch.setattr(onpage, "summary", lambda tid: env["summary"].pop(0) if env["summary"] else None)
    return env


def test_the_job_runs_lighthouse_then_waits_for_the_crawl(tech_env):
    asyncio.run(job_runner.run_technical_job("j1", {"audit_id": "a1"}))
    assert tech_env["saved"] == [("p1", "a1", 85)] and tech_env["started"] == ["apex.com"]
    statuses = [u["technical"]["status"] for u in tech_env["updates"]]
    assert statuses == ["crawling", "done"]
    assert tech_env["updates"][-1]["technical"]["health"] == 78


def test_a_crawl_that_outlasts_the_job_is_left_for_check_again(tech_env, monkeypatch):
    monkeypatch.setattr(job_runner, "TECHNICAL_POLL_SECONDS", 0)
    asyncio.run(job_runner.run_technical_job("j1", {"audit_id": "a1"}))
    assert [u["technical"]["status"] for u in tech_env["updates"]] == ["crawling"]


def test_a_refused_crawl_is_recorded_not_retried(tech_env, monkeypatch):
    def refuse(domain, **kw):
        raise onpage.OnPageUnavailable("DataForSEO: insufficient funds")
    monkeypatch.setattr(onpage, "start", refuse)
    asyncio.run(job_runner.run_technical_job("j1", {"audit_id": "a1"}))
    assert tech_env["updates"][-1]["technical"] == {
        "status": "failed", "error": "DataForSEO: insufficient funds",
        "updated_at": tech_env["updates"][-1]["technical"]["updated_at"]}


# ── Routes ────────────────────────────────────────────────────────────────────


def test_run_technical_queues_a_job_that_comes_back_to_the_section(client, queued, monkeypatch):
    import app.console.routes as routes

    monkeypatch.setattr(routes.store, "get_audit", lambda aid: {"prospect_id": "p1"})
    monkeypatch.setattr(routes.store, "get_prospect", lambda pid: {"business_name": "Apex"})
    csrf = sign_in(client)
    client.post("/console/audits/a1/technical", data={"csrf": csrf}, follow_redirects=False)
    kind, params, label = queued[0]
    assert kind == jobs.KIND_TECHNICAL and label == "Technical audit for Apex"
    assert params == {"audit_id": "a1", "return_to": "/console/audits/a1#technical"}


@pytest.mark.parametrize("finished, notice", [(True, "technical_done"), (False, "technical_pending")])
def test_check_again_reads_the_paid_for_crawl_without_starting_one(client, monkeypatch, finished, notice):
    import app.console.routes as routes

    written = []
    monkeypatch.setattr(routes.store, "get_audit", lambda aid: {"technical": {"task_id": "T1", "status": "crawling"}})
    monkeypatch.setattr(routes.store, "update_audit", lambda aid, f: written.append(f))
    monkeypatch.setattr(onpage, "summary", lambda tid: SUMMARY["tasks"][0]["result"][0] if finished else None)
    monkeypatch.setattr(onpage, "start", lambda *a, **k: pytest.fail("Check again must not start a crawl"))
    csrf = sign_in(client)
    r = client.post("/console/audits/a1/technical/check", data={"csrf": csrf}, follow_redirects=False)
    assert f"notice={notice}" in r.headers["location"] and r.headers["location"].endswith("#technical")
    assert bool(written) == finished


# ── The section ───────────────────────────────────────────────────────────────


def _with(**audit):
    return _prospect_page(audit={"report_slug": "x", **audit},
                          prospect={"website_url": "https://apex.com/"})


def test_the_section_shows_the_rings_the_link_and_the_run_button():
    record = {"scores": {"performance": 34, "accessibility": 81, "best_practices": 92, "seo": None},
              "metrics": {"lcp_ms": 6840, "lcp_source": "field"}, "issues": [],
              "psi_url": "https://pagespeed.web.dev/report?url=x", "measured_at": datetime(2026, 10, 9, tzinfo=timezone.utc)}
    page = _with(lighthouse=record)
    assert 'class="gauge poor" style="--v: 34"' in page and 'class="gauge good" style="--v: 92"' in page
    assert 'class="gauge none"' in page and "SEO: not measured out of 100" in page
    assert 'href="https://pagespeed.web.dev/report?url=x"' in page and "6.8 s" in page and "real visitors" in page
    assert 'action="/console/audits/a1/technical"' in page and "measured Oct 09, 2026" in page


def test_before_any_run_the_section_says_what_to_do():
    page = _with()
    assert "No Lighthouse result on this audit yet." in page and "No crawl yet." in page
    assert "pagespeed.web.dev/report?url=https%3A%2F%2Fapex.com%2F" in page, "the link works before a run"


def test_a_crawl_still_running_offers_check_again():
    page = _with(technical={"status": "crawling", "task_id": "T1", "max_pages": 100,
                            "started_at": datetime(2026, 10, 9)})
    assert 'action="/console/audits/a1/technical/check"' in page and "Crawling since Oct 09" in page


def test_the_public_report_never_takes_googles_screenshot():
    """Lighthouse evidence is its own kind, so the report's screenshot pick
    (kind == screenshot) can never land on it."""
    from app.report import publish

    src = open(publish.__file__).read()
    assert 'row.get("kind") == "screenshot"' in src and '"lighthouse"' not in src


# ── Sites behind a robot check ────────────────────────────────────────────────

EMPTY = {"crawl_progress": "finished", "crawl_status": {"pages_crawled": 0}, "domain_info": {"total_pages": 0},
         "page_metrics": None}


def test_a_browser_crawl_asks_for_rendering(dfs):
    sent = {}

    def handler(request):
        import json
        sent.update(json.loads(request.content)[0])
        return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T2"}]})

    onpage.start("apex.com", max_pages=50, browser=True, client=_http(handler))
    assert sent["enable_browser_rendering"] is True and sent["enable_javascript"] is True


def test_an_empty_crawl_is_retried_in_a_browser(tech_env, monkeypatch):
    starts = []
    monkeypatch.setattr(onpage, "start", lambda domain, **kw: starts.append(kw) or f"T{len(starts)}")
    tech_env["summary"][:] = [EMPTY, SUMMARY["tasks"][0]["result"][0]]
    asyncio.run(job_runner.run_technical_job("j1", {"audit_id": "a1"}))
    assert [k["browser"] for k in starts] == [False, True]
    assert starts[1]["max_pages"] == Config().onpage_browser_max_pages
    last = tech_env["updates"][-1]["technical"]
    assert last["status"] == "done" and last["browser"] is True and last["needs_browser"] is True


def test_empty_even_in_a_browser_is_blocked_not_retried_forever(tech_env, monkeypatch):
    starts = []
    monkeypatch.setattr(onpage, "start", lambda domain, **kw: starts.append(kw) or "T")
    tech_env["summary"][:] = [EMPTY, EMPTY, EMPTY]
    asyncio.run(job_runner.run_technical_job("j1", {"audit_id": "a1"}))
    assert len(starts) == 2 and tech_env["updates"][-1]["technical"]["status"] == "blocked"


def test_a_site_known_to_need_a_browser_skips_the_plain_crawl(tech_env, monkeypatch):
    starts = []
    monkeypatch.setattr(job_runner.store, "get_audit",
                        lambda aid: {"prospect_id": "p1", "technical": {"needs_browser": True}})
    monkeypatch.setattr(onpage, "start", lambda domain, **kw: starts.append(kw) or "T")
    asyncio.run(job_runner.run_technical_job("j1", {"audit_id": "a1"}))
    assert [k["browser"] for k in starts] == [True]


def test_check_again_on_an_empty_crawl_records_the_block_and_spends_nothing(client, monkeypatch):
    import app.console.routes as routes

    written = []
    monkeypatch.setattr(routes.store, "get_audit", lambda aid: {"technical": {"task_id": "T1", "status": "crawling"}})
    monkeypatch.setattr(routes.store, "update_audit", lambda aid, f: written.append(f))
    monkeypatch.setattr(onpage, "summary", lambda tid: EMPTY)
    monkeypatch.setattr(onpage, "start", lambda *a, **k: pytest.fail("Check again must not start a crawl"))
    csrf = sign_in(client)
    client.post("/console/audits/a1/technical/check", data={"csrf": csrf}, follow_redirects=False)
    assert written[0]["technical"]["status"] == "blocked" and written[0]["technical"]["needs_browser"] is True


def test_an_old_empty_crawl_reads_as_blocked_not_as_never_run():
    page = _with(technical={"status": "done", "health": None, "pages_crawled": 0, "issues": []})
    assert "No crawl yet." not in page and "The crawl got no pages: The site shows crawlers a robot check" in page


def test_a_browser_crawl_says_so():
    page = _with(technical={"status": "done", "health": 81, "pages_crawled": 12, "browser": True,
                            "issues": [], "site": []})
    assert "12 pages crawled in a real browser (the site has a robot check)" in page
