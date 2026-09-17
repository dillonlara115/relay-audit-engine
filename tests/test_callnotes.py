"""Call notes: built from the record, readable aloud, nothing invented."""

from __future__ import annotations

from datetime import datetime, timezone

from app.console import callnotes
from app.copy_rules import contains_forbidden_dash

DEFS = {
    "F3": {"section": "found", "title": "Review count", "sort_order": 3},
    "F4": {"section": "found", "title": "Average rating", "sort_order": 4},
    "C1": {"section": "chosen", "title": "Mobile", "sort_order": 1},
    "B2": {"section": "booked", "title": "Form health", "sort_order": 2},
    "B1": {"section": "booked", "title": "Online booking", "sort_order": 1},
}
CHECKS = [
    {"code": "F3", "status": "fail", "note": "33 Google reviews, under the 50 a homeowner expects."},
    {"code": "F4", "status": "pass", "note": "Rated 4.9 on Google."},
    {"code": "C1", "status": "pass", "note": "The site works on a phone."},
    {"code": "B2", "status": "fail", "note": "The contact form does not confirm anything was received."},
    {"code": "B1", "status": "fail", "note": "Nobody can book a time without waiting for a call back."},
]


def doc():
    return {"findings": [{"ordinal": i, "what_we_saw": f"Saw thing {i}.",
                          "what_it_means": f"It means {i}."} for i in range(1, 6)],
            "selected": [1, 2, 3]}


def build(**over):
    kw = dict(prospect={"business_name": "Apex Roofing", "city": "Fort Collins",
                        "gbp_phone": "(970) 224-1200", "domain": "apex.com"},
              audit={"segment": "Leaky Bucket"}, checks=CHECKS, definitions=DEFS,
              findings_doc=doc(), report_url="https://r/abc")
    kw.update(over)
    return callnotes.build(**kw)


def test_the_report_findings_are_what_to_raise_and_the_rest_is_held_back():
    n = build()
    assert [r["saw"] for r in n["raise"]] == ["Saw thing 1.", "Saw thing 2.", "Saw thing 3."]
    assert n["raise"][0]["means"] == "It means 1."
    assert n["raise_source"] == "report"
    assert n["more"] == ["Saw thing 4.", "Saw thing 5."]


def test_without_findings_the_failing_checks_stand_in_booked_first():
    n = build(findings_doc=None)
    assert n["raise_source"] == "checks"
    assert [r["saw"] for r in n["raise"]] == [
        "Nobody can book a time without waiting for a call back.",
        "The contact form does not confirm anything was received.",
        "33 Google reviews, under the 50 a homeowner expects.",
    ]
    assert n["more"] == []


def test_strengths_come_from_passing_checks_with_a_note():
    n = build()
    assert [s["title"] for s in n["strengths"]] == ["Average rating", "Mobile"]
    assert n["strengths"][0]["note"] == "Rated 4.9 on Google."


def test_the_angle_follows_the_segment_and_incomplete_is_honest():
    assert "slip away" in build()["angle"][0]
    n = build(audit={"segment": None})
    assert n["who"]["segment"] == "Incomplete"
    assert "could not finish" in n["angle"][0]


def test_where_they_stand_reads_the_ledger():
    assert build()["standing"].startswith("No email has gone out yet")
    n = build(touches=[{"ordinal": 1, "sent_at": datetime(2026, 9, 17, tzinfo=timezone.utc)}],
              replies=[{"received_at": datetime(2026, 9, 18, tzinfo=timezone.utc),
                        "intent": "interested", "excerpt": "Call me Thursday."}],
              intent_labels={"interested": "Interested"})
    assert n["standing"] == '1 email sent, the last on Sep 17. They replied: Interested. "Call me Thursday."'
    n = build(touches=[{"ordinal": 1}, {"ordinal": 2}])
    assert n["standing"] == "2 emails sent. No reply yet; this call is the follow-up."


def test_the_ask_carries_the_report_link_when_there_is_one():
    assert build()["ask"][0] == "Offer to send the write-up: https://r/abc"
    assert build(report_url=None)["ask"][0].startswith("Offer to send the write-up once")


def test_the_text_form_is_complete_and_clean():
    t = build()["text"]
    for part in ("CALL NOTES: Apex Roofing, Fort Collins", "(970) 224-1200 | apex.com",
                 "WHERE THEY STAND", "THE ANGLE", "OPEN WITH", "1. Saw thing 1.",
                 "IF THEY WANT MORE", "THE ASK", "RULES"):
        assert part in t, part
    assert not contains_forbidden_dash(t)


def test_nothing_is_invented_for_an_empty_record():
    n = build(prospect={}, audit={}, checks=[], definitions={}, findings_doc=None, report_url=None)
    assert n["raise"] == [] and n["strengths"] == [] and n["more"] == []
    assert n["who"]["phone"] == "" and n["who"]["business"] == ""
