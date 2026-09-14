"""Finding the address a report can actually be sent to.

Discovery runs over the crawl the fit gate already performed, so it costs zero
extra requests and stays inside the politeness budget in criteria section 8. A
prospect that has been gated has its contacts at the same moment, which is what
lets the call list carry a Contact column instead of a blank.

Nothing here contacts anybody. It reads pages already fetched and reports the
addresses on them. Ranking is by how much a human can do with the address: a
named person at the company's own domain outranks info@, which outranks a
gmail account in the footer, because the report is addressed to an owner.

Absent stays absent. A site with no address on it yields an empty list, never a
guessed firstname.lastname@domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from selectolax.parser import HTMLParser

from app.tools.crawl import SiteCrawl, registrable_host

# Deliberately stricter than the RFC. We would rather miss an exotic address
# than write a parse artifact into a prospect record.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,24}")

# `logo@2x.png` matches any sane email pattern. So does `sprite@3x.webp`. The
# TLD position is a file extension in both, so screen on that rather than
# trying to make the pattern clever.
_ASSET_SUFFIXES = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "ico", "bmp", "tiff",
    "css", "js", "json", "woff", "woff2", "ttf", "eot", "mp4", "webm", "pdf",
})

# Addresses that exist to absorb mail, not to answer it.
_JUNK_LOCAL = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "bounce", "bounces",
    "mailer-daemon", "postmaster", "abuse", "webmaster", "hostmaster",
    "privacy", "legal", "dmca", "unsubscribe", "notifications", "wordpress",
})

# Placeholder domains a template shipped with, and platform addresses that
# belong to the site's vendor rather than the roofer.
_JUNK_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yoursite.com", "yourcompany.com", "company.com", "email.com", "test.com",
    "sentry.io", "sentry-cdn.com", "wixpress.com", "wix.com", "squarespace.com",
    "godaddy.com", "wordpress.com", "automattic.com", "cloudflare.com",
    "googlegroups.com", "sentry.wixpress.com",
})

# A shared mailbox. Reaches the business, reaches nobody in particular.
_ROLE_LOCAL = frozenset({
    "info", "office", "sales", "contact", "contactus", "admin", "hello", "hi",
    "support", "service", "customerservice", "estimates", "estimate", "quotes",
    "quote", "scheduling", "schedule", "billing", "accounting", "accounts",
    "team", "inquiries", "inquiry", "enquiries", "help", "mail", "email",
    "roofing", "roofs", "dispatch", "frontdesk", "reception",
    # Observed on a live Colorado Springs sweep, both read as personal names.
    "projectbids", "bids", "ask", "getaquote", "getstarted", "appointments",
    "customercare", "newcustomer", "leads", "web", "website", "marketing",
})

KIND_PERSONAL = "personal"
KIND_ROLE = "role"

# Page paths where an address is more likely to be the one the business wants
# used. A footer address on a blog post is the same address, but a /contact
# page states intent.
_INTENT_FRAGMENTS = ("contact", "about", "team", "staff", "leadership", "our-story", "meet")

MAX_CONTACTS = 5


@dataclass(frozen=True)
class Contact:
    """One address observed on the site, with where it came from.

    `status` and `checked_at` stay empty here. Verification is a separate step
    with its own failure modes, and discovery must not imply it ran.
    """

    email: str
    source: str            # "mailto" | "jsonld" | "text"
    kind: str              # KIND_PERSONAL | KIND_ROLE
    own_domain: bool
    page_path: str = "/"

    @property
    def local(self) -> str:
        return self.email.split("@", 1)[0]

    @property
    def domain(self) -> str:
        return self.email.split("@", 1)[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "source": self.source,
            "kind": self.kind,
            "own_domain": self.own_domain,
            "page_path": self.page_path,
        }


def _looks_like_asset(email: str) -> bool:
    suffix = email.rsplit(".", 1)[-1].lower()
    return suffix in _ASSET_SUFFIXES


def _is_junk(email: str) -> bool:
    local, _, domain = email.partition("@")
    if local.lower() in _JUNK_LOCAL:
        return True
    if domain.lower() in _JUNK_DOMAINS:
        return True
    # A local part that is a long hex blob is a tracking or bounce address.
    return bool(re.fullmatch(r"[0-9a-f]{16,}", local.lower()))


def normalize(raw: str | None) -> str | None:
    """Canonical lowercase address, or None when the input is not one."""
    if not raw:
        return None
    text = unquote(str(raw).strip())
    if text.lower().startswith("mailto:"):
        text = text[7:]
    text = text.split("?", 1)[0].strip().strip(".,;:<>()[]\"'")
    match = _EMAIL.fullmatch(text)
    if not match:
        return None
    email = match.group(0).lower()
    if _looks_like_asset(email) or _is_junk(email):
        return None
    return email


def _kind_of(email: str) -> str:
    local = email.split("@", 1)[0]
    stem = re.split(r"[._\-+]", local)[0].lower()
    if local.lower() in _ROLE_LOCAL or stem in _ROLE_LOCAL:
        return KIND_ROLE
    return KIND_PERSONAL


def _mailto_hrefs(tree: HTMLParser) -> list[str]:
    # selectolax matches attribute values case-insensitively, so the
    # mailto:/MAILTO: pair returns each anchor twice. Same dedupe as tel:.
    return list(
        dict.fromkeys(
            node.attributes.get("href", "")
            for node in tree.css('a[href^="mailto:"], a[href^="MAILTO:"]')
            if node.attributes.get("href")
        )
    )


def _jsonld_emails(html: str) -> list[str]:
    """Addresses declared in structured data.

    Walked as raw text rather than parsed JSON: the blocks are already parsed
    properly for the schema checks, and a malformed block that breaks json
    still carries a readable address.
    """
    out: list[str] = []
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        blob = node.text() or ""
        for match in re.finditer(r'"email"\s*:\s*"([^"]+)"', blob, re.I):
            out.append(match.group(1))
    return out


def _visible_text(html: str) -> str:
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, template"):
        tag.decompose()
    body = tree.body or tree.root
    return body.text(separator=" ") if body is not None else ""


def _rank(contact: Contact) -> tuple:
    """Lower sorts first. Own domain, then a name, then stated intent."""
    return (
        0 if contact.own_domain else 1,
        0 if contact.kind == KIND_PERSONAL else 1,
        0 if any(f in contact.page_path.lower() for f in _INTENT_FRAGMENTS) else 1,
        {"mailto": 0, "jsonld": 1, "text": 2}.get(contact.source, 3),
        contact.email,
    )


def _same_site(domain: str, site_host: str) -> bool:
    """A subdomain counts, a suffix collision does not: mail.acme.com is Acme,
    notacme.com is not."""
    if not site_host:
        return False
    return domain == site_host or domain.endswith("." + site_host)


def extract_contacts(crawl: SiteCrawl, *, limit: int = MAX_CONTACTS) -> list[Contact]:
    """Every usable address on the crawled site, best first."""
    site_host = registrable_host(crawl.base_url) or ""
    found: dict[str, Contact] = {}

    def offer(raw: str, source: str, path: str) -> None:
        email = normalize(raw)
        if not email or email in found:
            return
        found[email] = Contact(
            email=email,
            source=source,
            kind=_kind_of(email),
            own_domain=_same_site(email.split("@", 1)[1], site_host),
            page_path=path,
        )

    for page in crawl.all_pages():
        if not page.html:
            continue
        path = urlparse(page.final_url or page.url).path or "/"
        tree = HTMLParser(page.html)
        for href in _mailto_hrefs(tree):
            offer(href, "mailto", path)
        for raw in _jsonld_emails(page.html):
            offer(raw, "jsonld", path)
        for match in _EMAIL.finditer(_visible_text(page.html)):
            offer(match.group(0), "text", path)

    return sorted(found.values(), key=_rank)[:limit]


def best(contacts: Iterable[Contact]) -> Contact | None:
    ranked = sorted(contacts, key=_rank)
    return ranked[0] if ranked else None
