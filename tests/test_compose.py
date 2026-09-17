"""The drafted email. Pure text, so every case is data in and a string out.

The properties that matter: it sends nothing (there is nothing to send with),
carries no forbidden dash, leaks no internal vocabulary, and fits a mailto
link every client hands off intact.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.console.compose import MAX_MAILTO, compose, mailto_url, touch_draft
from app.copy_rules import contains_forbidden_dash
from app.report.data import forbidden_terms_in

URL = "https://reports.relayforroofers.com/abcdefghijklmnop"


def finding(i, saw=None, means="It means jobs go elsewhere."):
    return {"ordinal": i, "what_we_saw": saw or f"Saw thing {i} on the site.",
            "what_it_means": means, "what_fixing_takes": "An afternoon."}


def doc(n=6):
    return {"findings": [finding(i) for i in range(1, n + 1)], "selected": [1, 2, 3]}


def body_of(url):
    return parse_qs(urlparse(url).query)["body"][0]


def test_touch_one_carries_the_link_and_the_three_chosen_findings():
    d = compose(ordinal=1, prospect={"business_name": "Apex Roofing", "city": "Denver",
                                     "owner_email": "dave@apex.com"},
                report_url=URL, findings_doc=doc())
    assert d.to == "dave@apex.com"
    assert d.subject == "Apex Roofing: three things costing you booked jobs"
    for line in ("Hi there,", "homeowner in Denver", URL, "1. Saw thing 1", "2. Saw thing 2",
                 "3. Saw thing 3", "yours to keep either way", "{Your name}", "Relay for Roofers"):
        assert line in d.body, line
    assert "Saw thing 4" not in d.body


def test_a_follow_up_carries_one_held_back_finding_and_the_link_again():
    d = compose(ordinal=3, prospect={"business_name": "Apex Roofing"}, report_url=URL,
                findings_doc=doc())
    assert d.subject.startswith("Re: ")
    assert "Saw thing 5" in d.body          # second held-back finding
    assert "Saw thing 4" not in d.body and "Saw thing 1" not in d.body
    assert URL in d.body and "One more thing I noticed" in d.body


def test_lines_are_separated_by_crlf_and_encoded_as_such():
    d = compose(ordinal=1, prospect={"business_name": "A"}, report_url=URL, findings_doc=doc())
    assert "\r\n" in d.body
    assert "%0D%0A" in mailto_url(d)


def test_the_link_uses_percent_encoding_not_plus():
    d = compose(ordinal=1, prospect={"business_name": "Apex Roofing", "owner_email": "d@x.com"},
                report_url=URL, findings_doc=doc())
    url = mailto_url(d)
    assert url.startswith("mailto:d@x.com?subject=Apex%20Roofing")
    assert "+" not in url.split("?", 1)[1].replace("%2B", "")


def test_no_address_yields_an_empty_to():
    d = compose(ordinal=1, prospect={"business_name": "A"}, report_url=URL, findings_doc=doc())
    assert d.to is None
    assert mailto_url(d).startswith("mailto:?subject=")


def test_an_em_dash_in_a_finding_is_sanitised_out():
    bad = {"findings": [finding(1, "Nobody can book — they wait."), finding(2), finding(3)],
           "selected": [1, 2, 3]}
    d = compose(ordinal=1, prospect={"business_name": "A"}, report_url=URL, findings_doc=bad)
    assert not contains_forbidden_dash(d.body)
    assert "Nobody can book, they wait." in d.body


def test_a_follow_up_that_leaks_internal_vocabulary_is_dropped_with_a_warning():
    leaky = {"findings": [finding(1), finding(2), finding(3),
                          finding(4, "Their segment is Leaky Bucket.")], "selected": [1, 2, 3]}
    d = compose(ordinal=2, prospect={"business_name": "A"}, report_url=URL, findings_doc=leaky)
    assert "segment" not in d.body.lower()
    assert not forbidden_terms_in(d.body)
    assert d.warnings and "internal vocabulary" in d.warnings[0]


def test_the_report_findings_never_leak_because_publish_already_gated_them():
    d = compose(ordinal=1, prospect={"business_name": "A"}, report_url=URL, findings_doc=doc())
    assert not forbidden_terms_in(d.body)


def test_three_long_findings_still_fit_the_link_budget():
    long = {"findings": [finding(i, "word " * 60) for i in range(1, 4)], "selected": [1, 2, 3]}
    d = compose(ordinal=1, prospect={"business_name": "Apex Roofing", "city": "Denver",
                                     "owner_email": "dave@apex.com"},
                report_url=URL, findings_doc=long)
    url = mailto_url(d)
    assert len(url) <= MAX_MAILTO
    assert URL in body_of(url), "the link is the last thing trimmed"
    assert "1. " in body_of(url), "at least one finding survives"


def test_a_missing_findings_document_still_yields_a_usable_first_email():
    d = compose(ordinal=1, prospect={"business_name": "A", "city": "Pueblo"}, report_url=URL,
                findings_doc=None)
    assert URL in d.body and "Three things stood out:" in d.body
    assert len(mailto_url(d)) < MAX_MAILTO


def test_the_signature_is_configurable_and_sanitised():
    d = touch_draft(ordinal=1, business_name="A", city="B", report_url=URL,
                    report_findings=[finding(1)], signature="Relay — for Roofers")
    assert d.body.endswith("Relay, for Roofers")
    assert not contains_forbidden_dash(d.body)


@pytest.mark.parametrize("ordinal", [1, 2, 3, 4])
def test_every_touch_carries_the_placeholder_the_operator_replaces(ordinal):
    d = compose(ordinal=ordinal, prospect={"business_name": "A"}, report_url=URL, findings_doc=doc())
    assert "{Your name}" in d.body
