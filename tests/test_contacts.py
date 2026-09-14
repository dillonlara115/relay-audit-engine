"""Contact discovery. Pure over a crawl, so every case is HTML in, addresses out.

No network and no DNS: verification is a separate module with its own tests.
"""

from __future__ import annotations

import pytest

from app.tools.contacts import (
    KIND_PERSONAL,
    KIND_ROLE,
    Contact,
    best,
    extract_contacts,
    normalize,
)
from app.tools.crawl import FetchResult, SiteCrawl


def page(url: str, html: str) -> FetchResult:
    return FetchResult(url=url, final_url=url, status=200, html=html)


def crawl(base: str = "https://whitakerroofing.com", **pages: str) -> SiteCrawl:
    home = page(base + "/", pages.pop("home", "<html><body></body></html>"))
    return SiteCrawl(
        base_url=base,
        homepage=home,
        pages={f"{base}/{name}": page(f"{base}/{name}", html) for name, html in pages.items()},
    )


# ── normalize ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("mailto:Dave@Whitaker-Roofing.com", "dave@whitaker-roofing.com"),
    ("MAILTO:dave@x.com?subject=Roof%20quote", "dave@x.com"),
    ("mailto:%64ave@x.com", "dave@x.com"),
    ("  dave@x.com.  ", "dave@x.com"),
    ("<dave@x.com>", "dave@x.com"),
])
def test_normalize_canonicalizes(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", [
    "logo@2x.png",         # the retina-asset false positive
    "sprite@3x.webp",
    "icons@2x.svg",
    "noreply@acme.com",
    "do-not-reply@acme.com",
    "postmaster@acme.com",
    "bob@example.com",     # template placeholder
    "x@yourdomain.com",
    "a1b2c3d4e5f6a7b8@acme.com",  # bounce hash
    "not-an-email",
    "",
    None,
])
def test_normalize_rejects_what_is_not_a_usable_address(raw):
    assert normalize(raw) is None


# ── discovery ─────────────────────────────────────────────────────────────────


def test_finds_a_mailto_on_the_homepage():
    site = crawl(home='<html><body><a href="mailto:dave@whitakerroofing.com">Email</a></body></html>')
    found = extract_contacts(site)
    assert [c.email for c in found] == ["dave@whitakerroofing.com"]
    assert found[0].source == "mailto"
    assert found[0].own_domain is True


def test_finds_an_address_in_structured_data():
    html = ('<html><head><script type="application/ld+json">'
            '{"@type":"RoofingContractor","email":"office@whitakerroofing.com"}'
            '</script></head><body></body></html>')
    found = extract_contacts(crawl(home=html))
    assert found[0].email == "office@whitakerroofing.com"
    assert found[0].source == "jsonld"


def test_finds_an_address_in_visible_text():
    site = crawl(home="<html><body><p>Reach us at dave@whitakerroofing.com any time.</p></body></html>")
    assert [c.email for c in extract_contacts(site)] == ["dave@whitakerroofing.com"]


def test_script_and_style_content_is_not_visible_text():
    html = ('<html><body><script>var t="tracking@vendor.com";</script>'
            '<style>@media print{}</style><p>hi</p></body></html>')
    assert extract_contacts(crawl(home=html)) == []


def test_the_same_address_twice_is_one_contact():
    html = ('<html><body><a href="mailto:dave@whitakerroofing.com">a</a>'
            '<a href="MAILTO:Dave@WhitakerRoofing.com">b</a>'
            '<p>dave@whitakerroofing.com</p></body></html>')
    found = extract_contacts(crawl(home=html))
    assert len(found) == 1
    assert found[0].source == "mailto"   # the strongest source wins


def test_a_site_with_no_address_yields_nothing_rather_than_a_guess():
    assert extract_contacts(crawl(home="<html><body><h1>Roofing</h1></body></html>")) == []


def test_unreachable_pages_are_skipped():
    site = SiteCrawl(
        base_url="https://whitakerroofing.com",
        homepage=FetchResult(url="https://whitakerroofing.com/", status=500, html=None),
    )
    assert extract_contacts(site) == []


# ── ranking ───────────────────────────────────────────────────────────────────


def test_a_named_person_outranks_a_shared_mailbox():
    html = ('<html><body><a href="mailto:info@whitakerroofing.com">a</a>'
            '<a href="mailto:dave@whitakerroofing.com">b</a></body></html>')
    found = extract_contacts(crawl(home=html))
    assert [c.email for c in found] == [
        "dave@whitakerroofing.com", "info@whitakerroofing.com",
    ]
    assert found[0].kind == KIND_PERSONAL
    assert found[1].kind == KIND_ROLE


def test_the_company_domain_outranks_a_gmail_in_the_footer():
    html = ('<html><body><a href="mailto:whitakerroofs@gmail.com">a</a>'
            '<a href="mailto:info@whitakerroofing.com">b</a></body></html>')
    assert [c.email for c in extract_contacts(crawl(home=html))] == [
        "info@whitakerroofing.com", "whitakerroofs@gmail.com",
    ]


def test_an_address_on_the_contact_page_outranks_the_same_kind_in_a_blog_footer():
    site = crawl(
        home="<html><body></body></html>",
        **{
            "blog": '<html><body><a href="mailto:zeke@whitakerroofing.com">z</a></body></html>',
            "contact": '<html><body><a href="mailto:adam@whitakerroofing.com">a</a></body></html>',
        },
    )
    assert [c.email for c in extract_contacts(site)][0] == "adam@whitakerroofing.com"


def test_a_subdomain_is_the_same_site_but_a_suffix_collision_is_not():
    html = ('<html><body><a href="mailto:a@mail.whitakerroofing.com">a</a>'
            '<a href="mailto:b@notwhitakerroofing.com">b</a></body></html>')
    found = {c.email: c.own_domain for c in extract_contacts(html and crawl(home=html))}
    assert found["a@mail.whitakerroofing.com"] is True
    assert found["b@notwhitakerroofing.com"] is False


def test_www_on_the_base_url_does_not_break_own_domain_matching():
    site = crawl(
        base="https://www.whitakerroofing.com",
        home='<html><body><a href="mailto:dave@whitakerroofing.com">d</a></body></html>',
    )
    assert extract_contacts(site)[0].own_domain is True


def test_the_list_is_capped():
    links = "".join(f'<a href="mailto:person{i}@whitakerroofing.com">x</a>' for i in range(12))
    assert len(extract_contacts(crawl(home=f"<html><body>{links}</body></html>"))) == 5


def test_best_returns_none_on_an_empty_list():
    assert best([]) is None


def test_to_dict_carries_no_status():
    """Discovery must not imply verification ran."""
    contact = Contact(email="a@b.com", source="mailto", kind=KIND_PERSONAL, own_domain=True)
    assert "status" not in contact.to_dict()
