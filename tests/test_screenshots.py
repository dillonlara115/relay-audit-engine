"""Single-prospect jobs come back to the prospect; screenshots can be retaken
or replaced by hand."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app import job_runner, jobs
from app.console import views
from tests.test_console import _prospect_page, client, queued, sign_in  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64


# ── Jobs started from one prospect return there ───────────────────────────────


def _job(status="done", **params):
    return {"job_id": "j1", "kind": "draft", "status": status, "label": "Draft findings for Apex",
            "params": {"batch_id": "b1", **params}, "result": {"batch_id": "b1"}}


def test_a_finished_single_draft_links_back_to_the_prospect_not_the_call_list():
    page = views.render_job(_job(return_to="/console/audits/a1#findings"), csrf="t")
    assert 'href="/console/audits/a1#findings">Back to the prospect' in page
    assert "Open call list" not in page


def test_a_running_single_draft_redirects_when_it_finishes():
    page = views.render_job(_job(status="running", return_to="/console/audits/a1#findings"), csrf="t")
    assert 'data-return="/console/audits/a1#findings"' in page
    assert "window.location.href = back" in page


@pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example", "/console//evil", "/elsewhere"])
def test_only_a_console_path_is_followed(bad):
    page = views.render_job(_job(return_to=bad), csrf="t")
    assert "data-return" not in page and "Open call list" in page


def test_a_batch_draft_still_offers_the_call_list():
    assert "Open call list" in views.render_job(_job(), csrf="t")


def test_drafting_one_prospect_names_it_and_comes_back(client, queued, monkeypatch):
    import app.console.routes as routes

    monkeypatch.setattr(routes.store, "get_audit", lambda aid: {"batch_id": "b1", "prospect_id": "p1"})
    monkeypatch.setattr(routes.store, "get_prospect", lambda pid: {"business_name": "Patriot Roofing"})
    csrf = sign_in(client)
    client.post("/console/audits/a1/draft", data={"csrf": csrf}, follow_redirects=False)
    kind, params, label = queued[0]
    assert kind == jobs.KIND_DRAFT and label == "Draft findings for Patriot Roofing"
    assert params["only_audit_id"] == "a1" and params["return_to"] == "/console/audits/a1#findings"


# ── Retaking a screenshot ─────────────────────────────────────────────────────


def test_retake_queues_a_job_that_returns_to_the_screenshots(client, queued, monkeypatch):
    import app.console.routes as routes

    monkeypatch.setattr(routes.store, "get_audit", lambda aid: {"prospect_id": "p1"})
    monkeypatch.setattr(routes.store, "get_prospect", lambda pid: {"business_name": "Apex"})
    csrf = sign_in(client)
    client.post("/console/audits/a1/screenshot/retake", data={"csrf": csrf, "mode": "viewport"},
                follow_redirects=False)
    kind, params, label = queued[0]
    assert kind == jobs.KIND_SCREENSHOT and label == "Retake screenshot for Apex"
    assert params == {"audit_id": "a1", "mode": "viewport", "return_to": "/console/audits/a1#evidence"}


class _Shot:
    def __init__(self, ok=True):
        self.ok, self.error, self.screenshot_mime = ok, None if ok else "timeout", "image/jpeg"

    def screenshot(self):
        return JPEG if self.ok else None


@pytest.fixture()
def shot_env(monkeypatch):
    from app.store import evidence as evidence_store
    from app.tools import render as render_mod

    seen = {"render": [], "replaced": []}
    monkeypatch.setattr(job_runner.jobs, "log", lambda job_id, line: None)
    monkeypatch.setattr(job_runner.store, "get_audit",
                        lambda aid: {"prospect_id": "p1", "report_slug": "slug1"})
    monkeypatch.setattr(job_runner.store, "get_prospect", lambda pid: {"website_url": "https://apex.com/"})

    async def fake_render(url, **kw):
        seen["render"].append((url, kw))
        return seen.get("result", _Shot())
    monkeypatch.setattr(render_mod, "render", fake_render)
    monkeypatch.setattr(evidence_store, "replace_screenshot",
                        lambda pid, aid, data, **kw: seen["replaced"].append((pid, aid, data, kw)) or "p")
    return seen


def test_the_retake_job_renders_on_mobile_and_replaces_the_screenshot(shot_env):
    result = asyncio.run(job_runner.run_screenshot_job("j1", {"audit_id": "a1", "mode": "viewport"}))
    url, kw = shot_env["render"][0]
    assert url == "https://apex.com/" and kw == {"screenshot": "viewport", "image_format": "jpeg"}
    pid, aid, data, opts = shot_env["replaced"][0]
    assert (pid, aid, data) == ("p1", "a1", JPEG)
    assert opts == {"content_type": "image/jpeg", "source": "retaken", "report_slug": "slug1"}
    assert result == {"audit_id": "a1"}


def test_a_failed_render_fails_the_job_and_keeps_the_old_screenshot(shot_env):
    shot_env["result"] = _Shot(ok=False)
    with pytest.raises(RuntimeError, match="could not capture"):
        asyncio.run(job_runner.run_screenshot_job("j1", {"audit_id": "a1"}))
    assert shot_env["replaced"] == []


# ── Uploading one ─────────────────────────────────────────────────────────────


@pytest.fixture()
def upload_env(client, monkeypatch):
    import app.console.routes as routes
    from app.store import evidence as evidence_store

    saved = []
    monkeypatch.setattr(routes.store, "get_audit", lambda aid: {"prospect_id": "p1", "report_slug": "slug1"})
    monkeypatch.setattr(evidence_store, "replace_screenshot",
                        lambda pid, aid, data, **kw: saved.append((pid, aid, data, kw)) or "p")
    return saved


def _upload(client, csrf, data, name="shot.png"):
    return client.post("/console/audits/a1/screenshot/upload", data={"csrf": csrf},
                       files={"file": (name, data, "image/png")}, follow_redirects=False)


@pytest.mark.parametrize("data, mime", [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_an_uploaded_image_replaces_the_screenshot_and_the_reports(client, upload_env, data, mime):
    csrf = sign_in(client)
    r = _upload(client, csrf, data)
    assert "notice=screenshot_replaced" in r.headers["location"] and r.headers["location"].endswith("#evidence")
    pid, aid, stored, kw = upload_env[0]
    assert (pid, aid, stored) == ("p1", "a1", data)
    assert kw == {"content_type": mime, "source": "uploaded", "report_slug": "slug1"}


def test_the_files_own_bytes_decide_not_its_name(client, upload_env):
    csrf = sign_in(client)
    r = _upload(client, csrf, b"<svg onload=alert(1)>", name="looks.png")
    assert "notice=screenshot_rejected" in r.headers["location"] and upload_env == []


def test_an_oversized_upload_is_refused(client, upload_env):
    import app.console.routes as routes

    csrf = sign_in(client)
    r = _upload(client, csrf, PNG + b"\x00" * routes.SCREENSHOT_MAX_BYTES)
    assert "over+10+MB" in r.headers["location"] and upload_env == []


def test_an_upload_needs_a_fresh_form(client, upload_env):
    sign_in(client)
    assert _upload(client, "stale", PNG).status_code == 403 and upload_env == []


# ── Replacing in storage ──────────────────────────────────────────────────────


class _Ref:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def delete(self):
        self.store.pop(self.key)


class _Snap:
    def __init__(self, store, key):
        self.id, self._row, self.reference = key, store[key], _Ref(store, key)

    def to_dict(self):
        return self._row


def test_replacing_keeps_one_screenshot_and_refreezes_a_published_report(monkeypatch):
    from app.store import evidence as evidence_store

    docs = {"homepage.jpg": {"kind": "screenshot"}, "psi.json": {"kind": "payload"}}
    calls = {"uploads": [], "frozen": [], "updates": []}

    class _Coll:
        def document(self, _):
            return self

        def collection(self, _):
            return self

        def stream(self):
            return [_Snap(docs, k) for k in list(docs)]

    class _Client:
        def collection(self, _):
            return _Coll()

    monkeypatch.setattr(evidence_store.store, "get_client", lambda: _Client())
    monkeypatch.setattr(evidence_store, "upload", lambda pid, aid, name, payload, **kw:
                        calls["uploads"].append((name, kw)) or evidence_store.EvidenceRef(
                            f"evidence/{pid}/{aid}/{name}", "screenshot", "C17"))
    monkeypatch.setattr(evidence_store, "freeze_for_report",
                        lambda path, slug: calls["frozen"].append((path, slug)) or f"reports/{slug}/x")
    monkeypatch.setattr(evidence_store.store, "update_audit",
                        lambda aid, fields: calls["updates"].append((aid, fields)))

    evidence_store.replace_screenshot("p1", "a1", PNG, content_type="image/png", source="uploaded",
                                      report_slug="slug1")
    assert "homepage.jpg" not in docs and "psi.json" in docs, "the old jpg goes, other evidence stays"
    name, kw = calls["uploads"][0]
    assert name == "homepage.png" and kw["kind"] == "screenshot" and kw["extra"] == {"source": "uploaded"}
    assert calls["frozen"] == [("evidence/p1/a1/homepage.png", "slug1")]
    assert calls["updates"] == [("a1", {"report_screenshot_path": "reports/slug1/x"})]


def test_the_screenshots_dialog_offers_both_and_warns_about_a_published_report():
    page = _prospect_page(audit={"report_slug": "abcdefghijklmnop"})
    assert 'action="/console/audits/a1/screenshot/retake"' in page
    assert 'action="/console/audits/a1/screenshot/upload" enctype="multipart/form-data"' in page
    assert "It also replaces the screenshot on the published report." in page
    assert "published report" not in _prospect_page().split('id="evidence"', 1)[1].split("</dialog>", 1)[0]


def test_a_replaced_screenshot_says_where_it_came_from():
    page = views.render_audit(audit={"audit_id": "a1", "scores": {}}, prospect={}, checks=[], definitions={},
                              findings=None, csrf="t",
                              evidence=[{"kind": "screenshot", "size_bytes": 2048, "url": "https://x/y.png",
                                         "source": "uploaded", "captured_at": datetime(2026, 10, 9)}])
    assert "2 KB uploaded Oct 09" in page
