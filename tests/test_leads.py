"""The leads view: who has been contacted, and where each one stands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import outreach
from app.console import leads, views
from tests.test_console import client, sign_in  # noqa: F401 - the fixture and helper

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def pub(pid="p1", audit_id="a1", **kw):
    return {"audit_id": audit_id, "prospect_id": pid, "report_slug": "slug" + pid,
            "published_at": NOW - 5 * DAY, "segment": "Leaky Bucket", "scores": {"total": 40}, **kw}


def seq_after(sent=1, *, sent_at=NOW - DAY, status=None, **kw):
    s = outreach.open_sequence("p1", audit_id="a1")
    for _ in range(sent):
        s = outreach.advance(s, sent_at=sent_at)
    row = s.to_dict()
    if status:
        row["status"] = status
    row.update(kw)
    return row


def email(n=1, when=NOW - DAY):
    return ("p1", {"channel": "email", "ordinal": n, "sent_at": when})


def reply(intent, when=NOW):
    return ("p1", {"intent": intent, "received_at": when, "excerpt": "Sure, call me Thursday."})


def build(*, published=(pub(),), sequences=(), touches=(), replies=(), deals=None, prospect=None):
    prospects = {"p1": {"business_name": "Patriot Roofing", "city": "Pueblo", **(prospect or {})}}
    return leads.build(published=list(published), sequences=list(sequences), prospects=prospects,
                       touches=list(touches), replies=list(replies), deals=deals or {}, now=NOW)


def one(**kw):
    rows = build(**kw)
    assert len(rows) == 1
    return rows[0]


# ── Stage, from the ledger ────────────────────────────────────────────────────


def test_a_published_report_with_nothing_sent_is_not_contacted_and_needs_action():
    r = one()
    assert (r["stage"], r["next"], r["attention"]) == ("not_contacted", "Send email 1", True)
    assert r["last"] is None and r["audit_id"] == "a1"


def test_a_sent_email_puts_the_lead_in_sequence_with_the_next_due_date():
    r = one(sequences=[seq_after(1)], touches=[email(1)])
    assert r["stage"] == "in_sequence"
    assert r["next"] == "Email 2 of 4 due Oct 11" and not r["attention"]
    assert r["last"]["text"] == "Email 1 · Oct 08"


def test_an_email_that_has_come_due_needs_action():
    r = one(sequences=[seq_after(1, sent_at=NOW - 10 * DAY)], touches=[email(1, NOW - 10 * DAY)])
    assert r["next"] == "Email 2 of 4 due now" and r["attention"]


def test_a_reply_moves_the_lead_to_replied_and_asks_for_a_stage():
    r = one(sequences=[seq_after(1)], touches=[email(1)], replies=[reply(outreach.INTERESTED)])
    assert (r["stage"], r["stage_detail"]) == ("replied", ""), "the reply pill names the intent once"
    assert r["attention"] and r["reply"]["kind"] == "ok" and "Thursday" in r["reply"]["excerpt"]


def test_not_interested_closes_the_lead():
    r = one(touches=[email(1)], replies=[reply(outreach.NOT_INTERESTED)])
    assert (r["stage"], r["stage_detail"], r["attention"]) == ("closed", "Not interested", False)


def test_a_finished_sequence_with_no_reply_is_closed_with_its_reason():
    r = one(sequences=[seq_after(4)], touches=[email(n) for n in range(1, 5)])
    assert r["stage"] == "closed" and r["stage_detail"] == "sequence complete, no reply"


def test_a_suppressed_prospect_is_closed_whatever_else_happened():
    r = one(touches=[email(1)], replies=[reply(outreach.INTERESTED)], prospect={"suppressed": True})
    assert (r["stage"], r["stage_detail"]) == ("closed", "Suppressed")


def test_a_waiting_sequence_says_what_it_needs():
    r = one(sequences=[seq_after(1, status=outreach.WAITING, last_intent=outreach.OTHER)],
            touches=[email(1)])
    assert r["next"] == "Needs you: a reply to read" and r["attention"]


# ── Stage, set by hand ────────────────────────────────────────────────────────


@pytest.mark.parametrize("hand, stage, detail", [("call_booked", "call_booked", ""),
                                                 ("proposal_sent", "proposal_sent", ""),
                                                 ("won", "won", ""), ("lost", "closed", "Lost")])
def test_a_hand_set_stage_outranks_the_ledger(hand, stage, detail):
    r = one(touches=[email(1)], replies=[reply(outreach.INTERESTED)], deals={"p1": {"stage": hand}})
    assert (r["stage"], r["stage_detail"], r["hand_set"]) == (stage, detail, hand)


# ── Who is a lead ─────────────────────────────────────────────────────────────


def test_a_text_alone_makes_a_lead_even_without_a_report():
    rows = build(published=[], touches=[("p1", {"channel": "sms", "sent_at": NOW})])
    assert len(rows) == 1 and rows[0]["stage"] == "in_sequence" and rows[0]["last"]["icon"] == "message"


def test_an_unpublished_uncontacted_prospect_is_not_a_lead():
    assert build(published=[]) == []


def test_the_home_audit_is_the_one_the_outreach_runs_on():
    older, newer = pub(audit_id="a1"), pub(audit_id="a2", published_at=NOW)
    assert leads.home_audit("p1", {"audit_id": "a1"}, [older, newer])["audit_id"] == "a1"
    assert leads.home_audit("p1", None, [older, newer])["audit_id"] == "a2"


def test_an_incoming_call_is_not_counted_as_contacting_them():
    rows = build(touches=[("p1", {"channel": "call", "direction": "incoming", "sent_at": NOW})])
    assert rows[0]["stage"] == "not_contacted"


# ── Counts, tabs, board ───────────────────────────────────────────────────────


def _three():
    return (build() + build(published=[pub("p2", "b2")])  # two not contacted
            + one_in_sequence())


def one_in_sequence():
    rows = leads.build(published=[pub("p3", "c3")], sequences=[], prospects={"p3": {"business_name": "Summit"}},
                       touches=[("p3", {"channel": "email", "ordinal": 1, "sent_at": NOW})],
                       replies=[], deals={}, now=NOW)
    return rows


def test_counts_tabs_and_board_columns_agree():
    rows = _three()
    c = leads.counts(rows)
    assert c["all"] == 3 and c["not_contacted"] == 2 and c["in_sequence"] == 1 and c["action"] == 2
    assert [r["name"] for r in leads.filter_rows(rows, tab="in_sequence")] == ["Summit"]
    assert leads.filter_rows(rows, q="summ")[0]["name"] == "Summit"
    cols = leads.columns(rows)
    assert [col["key"] for col in cols] == [s.key for s in leads.STAGES], "every stage, even empty"


def test_rows_needing_action_come_first():
    rows = _three()
    assert [r["attention"] for r in rows] == [True, True, False]


# ── The page ──────────────────────────────────────────────────────────────────


def test_the_table_lands_on_needs_action_and_offers_the_board():
    page = views.render_leads(_three(), csrf="t")
    assert 'tab-active on" href="?view=table&amp;tab=action" aria-selected="true">Needs action <b>2</b>' in page
    assert 'href="?view=board&amp;tab=action"' in page
    assert page.count('action="/console/leads/') == 2, "only the rows on this tab"
    assert 'href="/console/leads"' in page, "Leads is in the nav"


def test_the_board_shows_every_stage_as_a_column():
    page = views.render_leads(_three(), view="board", tab="all", csrf="t")
    assert page.count('<section class="board-col"') == len(leads.STAGES)
    assert page.count('<article class="lead-card') == 3


def test_choosing_a_deal_stage_warns_that_follow_ups_stop():
    page = views.render_leads(_three(), csrf="t")
    assert "function stageConfirm(form)" in page and "follow-up emails still scheduled will stop" in page
    assert '"call_booked"' in page and '"auto"' not in page.split("MANUAL_STAGES =", 1)[1].split(";", 1)[0]


# ── Routes ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def deal_store(monkeypatch):
    import app.console.routes as routes

    written = {"deals": [], "sequences": []}
    open_seq = seq_after(1, sent_at=datetime.now(timezone.utc))
    monkeypatch.setattr(routes.store, "set_deal_stage", lambda pid, stage: written["deals"].append((pid, stage)))
    monkeypatch.setattr(routes.store, "get_sequence", lambda pid: open_seq)
    monkeypatch.setattr(routes.store, "save_sequence", lambda seq: written["sequences"].append(seq))
    for name, value in (("published_audits", [pub()]), ("all_sequences", [open_seq]), ("all_touches", [email(1)]),
                        ("all_replies", []), ("all_deals", {}),
                        ("prospects_by_id", {"p1": {"business_name": "Patriot Roofing", "city": "Pueblo"}})):
        monkeypatch.setattr(routes.store, name, lambda *a, _v=value, **k: _v)
    return written


def test_the_leads_page_is_gated_and_renders(client, deal_store):
    assert client.get("/console/leads").status_code == 401
    sign_in(client)
    page = client.get("/console/leads?view=board").text
    assert "Patriot Roofing" in page and '<section class="board-col"' in page


def test_a_deal_stage_is_recorded_and_stops_the_follow_ups(client, deal_store):
    csrf = sign_in(client)
    r = client.post("/console/leads/p1/stage", data={"csrf": csrf, "stage": "call_booked"},
                    follow_redirects=False, headers={"referer": "http://testserver/console/leads"})
    assert r.status_code == 303 and "notice=stage_set" in r.headers["location"]
    assert deal_store["deals"] == [("p1", "call_booked")]
    closed = deal_store["sequences"][0]
    assert closed.status == outreach.CLOSED and closed.next_due_at is None
    assert closed.closed_reason == "deal: call booked"


def test_back_to_automatic_clears_the_stage_and_leaves_the_sequence_alone(client, deal_store):
    csrf = sign_in(client)
    client.post("/console/leads/p1/stage", data={"csrf": csrf, "stage": "auto"}, follow_redirects=False)
    assert deal_store["deals"] == [("p1", None)] and deal_store["sequences"] == []


def test_an_unknown_stage_or_a_stale_form_changes_nothing(client, deal_store):
    csrf = sign_in(client)
    r = client.post("/console/leads/p1/stage", data={"csrf": csrf, "stage": "maybe"}, follow_redirects=False)
    assert "notice=stage_rejected" in r.headers["location"]
    assert client.post("/console/leads/p1/stage", data={"csrf": "wrong", "stage": "won"}).status_code == 403
    assert deal_store["deals"] == []


# ── On the prospect page ──────────────────────────────────────────────────────


def _prospect(deal=None):
    return views.render_audit(audit={"audit_id": "a1", "prospect_id": "p1", "scores": {}, "report_slug": "x"},
                              prospect={"business_name": "Patriot Roofing"}, checks=[], definitions={},
                              findings={"status": "approved", "selected": [1, 2, 3],
                                        "findings": [{"ordinal": i, "what_we_saw": f"s{i}"} for i in range(1, 7)]},
                              evidence=[], csrf="t", deal=deal)


def test_the_prospect_page_sets_the_deal_stage_too():
    page = _prospect()
    assert 'action="/console/leads/p1/stage"' in page and '<option value="auto" selected>' in page


def test_a_hand_set_stage_replaces_the_next_email_prompt():
    page = _prospect(deal={"stage": "call_booked"})
    assert "Deal stage: Call booked" in page and "Write email" not in page
    assert '<option value="call_booked" selected>' in page
