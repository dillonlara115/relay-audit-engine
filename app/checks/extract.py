"""Parse a crawl once into the facts the checks read.

Seventeen checks over twenty five pages is four hundred parses if every check
walks the DOM itself. This module walks it once and hands out plain data, which
also means a check is a comparison rather than a scraper and can be tested
against a string.

Nothing here decides pass or fail. It reports what is on the page.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from app.tools.crawl import FetchResult, SiteCrawl, parse_w3c_date, text_of
from app.tools.phones import Phone, parse_phone

# Field types that a homeowner never fills in, so they do not count as friction.
_INVISIBLE_FIELD_TYPES = frozenset({"hidden", "submit", "button", "image", "reset"})

# Common honeypot and anti-spam field names. Counting them as friction would
# punish a site for defending itself.
_HONEYPOT_HINTS = ("honeypot", "hp_", "_hp", "nonce", "csrf", "token", "captcha", "recaptcha",
                   "gotcha", "leaveblank", "leave-blank", "url_field", "website_field")

_HEADER_SELECTORS = "header, nav, [class*=header], [id*=header], [class*=topbar], [class*=top-bar]"

_DATE_META = (
    'meta[property="article:modified_time"]',
    'meta[property="article:published_time"]',
    'meta[name="date"]',
    'meta[itemprop="dateModified"]',
    'meta[itemprop="datePublished"]',
)


@dataclass(frozen=True)
class FieldFacts:
    name: str
    kind: str
    required: bool

    @property
    def visible(self) -> bool:
        if self.kind in _INVISIBLE_FIELD_TYPES:
            return False
        lowered = self.name.lower()
        return not any(hint in lowered for hint in _HONEYPOT_HINTS)


@dataclass(frozen=True)
class FormFacts:
    action: str | None
    method: str
    fields: tuple[FieldFacts, ...]
    has_required: bool

    @property
    def visible_fields(self) -> tuple[FieldFacts, ...]:
        return tuple(f for f in self.fields if f.visible)

    @property
    def questions(self) -> tuple[str, ...]:
        """Distinct questions, not distinct inputs.

        A checkbox group renders one input per option and a form builder names
        them all `form_fields[x][]`, so counting inputs turns one question into
        six. Radio groups have the same shape. Unnamed inputs cannot be grouped
        and each count once.
        """
        seen: list[str] = []
        for index, field_ in enumerate(self.visible_fields):
            key = field_.name.strip().removesuffix("[]") or f"__unnamed_{index}"
            if key not in seen:
                seen.append(key)
        return tuple(seen)

    @property
    def field_count(self) -> int:
        return len(self.questions)

    @property
    def looks_like_search(self) -> bool:
        names = " ".join(f.name.lower() for f in self.fields)
        kinds = {f.kind for f in self.fields}
        return "search" in names or "search" in (self.action or "").lower() or kinds == {"search"}


@dataclass
class PageFacts:
    url: str
    path: str
    title: str
    text: str          # lowercased visible text
    html: str          # raw source, lowercased
    jsonld: tuple[Any, ...] = ()
    forms: tuple[FormFacts, ...] = ()
    tel_hrefs: tuple[str, ...] = ()
    header_tel_hrefs: tuple[str, ...] = ()
    dates: tuple[datetime, ...] = ()
    rendered: bool = False   # True when this came from the browser, not the source


@dataclass
class SiteFacts:
    """Everything the on-page checks need, parsed once."""

    homepage: PageFacts | None
    pages: tuple[PageFacts, ...] = ()
    sitemap_paths: tuple[str, ...] = ()
    sitemap_lastmod: dict[str, datetime] = field(default_factory=dict)
    reachable: bool = False
    robots_blocked: tuple[str, ...] = ()

    # Corpora, built once.
    text: str = ""
    html: str = ""

    def __post_init__(self) -> None:
        if not self.text:
            self.text = " ".join(p.text for p in self.pages)
        if not self.html:
            self.html = " ".join(p.html for p in self.pages)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(p.path for p in self.pages)

    @property
    def all_paths(self) -> tuple[str, ...]:
        """Crawled plus advertised in the sitemap. A page we did not fetch is
        still a page the site publishes, which is what F10 is asking about."""
        return tuple(dict.fromkeys([*self.paths, *self.sitemap_paths]))

    @property
    def jsonld(self) -> tuple[Any, ...]:
        return tuple(block for page in self.pages for block in page.jsonld)

    @property
    def forms(self) -> tuple[FormFacts, ...]:
        return tuple(form for page in self.pages for form in page.forms)

    @property
    def tel_hrefs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(href for page in self.pages for href in page.tel_hrefs))

    def phones(self) -> list[Phone]:
        out: list[Phone] = []
        seen: set[str] = set()
        for href in self.tel_hrefs:
            phone = parse_phone(href)
            if phone and phone.e164 not in seen:
                seen.add(phone.e164)
                out.append(phone)
        return out

    def newest_date(self) -> datetime | None:
        candidates = [d for page in self.pages for d in page.dates]
        candidates.extend(self.sitemap_lastmod.values())
        return max(candidates) if candidates else None

    def has_any(self, needles: Iterable[str]) -> bool:
        return any(needle in self.text for needle in needles)

    def html_has_any(self, needles: Iterable[str]) -> bool:
        return any(needle in self.html for needle in needles)

    def matching_paths(self, fragments: Iterable[str]) -> list[str]:
        fragments = tuple(fragments)
        return [p for p in self.all_paths if any(f in p.lower() for f in fragments)]


# ── Parsing ───────────────────────────────────────────────────────────────────


def _json_blocks(tree: HTMLParser) -> tuple[Any, ...]:
    """JSON-LD blocks, flattened through @graph and top-level arrays.

    Malformed JSON-LD is extremely common and is not a finding on its own, so a
    block that will not parse is dropped rather than raised.
    """
    out: list[Any] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = (node.text() or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        stack = [parsed]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                out.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
    return tuple(out)


def _forms(tree: HTMLParser) -> tuple[FormFacts, ...]:
    out: list[FormFacts] = []
    for node in tree.css("form"):
        fields: list[FieldFacts] = []
        has_required = False
        for control in node.css("input, select, textarea"):
            attrs = control.attributes
            kind = (attrs.get("type") or ("textarea" if control.tag == "textarea" else "text")).lower()
            required = "required" in attrs or (attrs.get("aria-required") or "").lower() == "true"
            has_required = has_required or required
            fields.append(
                FieldFacts(name=attrs.get("name") or attrs.get("id") or "", kind=kind, required=required)
            )
        out.append(
            FormFacts(
                action=node.attributes.get("action"),
                method=(node.attributes.get("method") or "get").lower(),
                fields=tuple(fields),
                has_required=has_required,
            )
        )
    return tuple(out)


def _tel_hrefs(tree: HTMLParser) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """All tel: hrefs, and the subset that sits in a header or nav."""
    # selectolax matches attribute values case-insensitively, so the tel:/TEL:
    # pair returns each anchor twice. Dedupe rather than double-count.
    every = tuple(
        dict.fromkeys(
            node.attributes.get("href", "")
            for node in tree.css('a[href^="tel:"], a[href^="TEL:"]')
            if node.attributes.get("href")
        )
    )
    header: list[str] = []
    for region in tree.css(_HEADER_SELECTORS):
        for node in region.css('a[href^="tel:"], a[href^="TEL:"]'):
            href = node.attributes.get("href")
            if href:
                header.append(href)
    return every, tuple(dict.fromkeys(header))


def _dates(tree: HTMLParser, jsonld: tuple[Any, ...]) -> tuple[datetime, ...]:
    """Published and modified timestamps a page declares about itself."""
    found: list[datetime] = []

    for selector in _DATE_META:
        for node in tree.css(selector):
            parsed = parse_w3c_date(node.attributes.get("content"))
            if parsed:
                found.append(parsed)

    for node in tree.css("time[datetime]"):
        parsed = parse_w3c_date(node.attributes.get("datetime"))
        if parsed:
            found.append(parsed)

    for block in jsonld:
        if not isinstance(block, dict):
            continue
        for key in ("dateModified", "datePublished", "uploadDate"):
            value = block.get(key)
            if isinstance(value, str):
                parsed = parse_w3c_date(value)
                if parsed:
                    found.append(parsed)

    now = datetime.now(timezone.utc)
    # A page claiming to be from the future is a broken template, not fresh copy.
    return tuple(d for d in found if d <= now)


def parse_page(result: FetchResult) -> PageFacts | None:
    if not result.ok or not result.html:
        return None
    url = result.final_url or result.url
    tree = HTMLParser(result.html)
    jsonld = _json_blocks(tree)
    every_tel, header_tel = _tel_hrefs(tree)
    title_node = tree.css_first("title")
    return PageFacts(
        url=url,
        path=urlparse(url).path or "/",
        title=(title_node.text() if title_node else "").strip(),
        text=text_of(result.html),
        html=result.html.lower(),
        jsonld=jsonld,
        forms=_forms(tree),
        tel_hrefs=every_tel,
        header_tel_hrefs=header_tel,
        dates=_dates(tree, jsonld),
    )


def build(crawl: SiteCrawl) -> SiteFacts:
    pages = [p for p in (parse_page(r) for r in crawl.all_pages()) if p is not None]
    homepage = next(
        (p for p in pages if p.url in (crawl.homepage.final_url, crawl.homepage.url)),
        pages[0] if pages else None,
    )
    return SiteFacts(
        homepage=homepage,
        pages=tuple(pages),
        sitemap_paths=tuple(urlparse(u).path or "/" for u in crawl.sitemap_urls),
        sitemap_lastmod=dict(crawl.sitemap_lastmod),
        reachable=crawl.reachable,
        robots_blocked=tuple(crawl.robots_blocked),
    )



# A site can serve a shell and build the page with JavaScript. Measured on a
# real prospect: 4 characters of visible text in the source across five pages,
# against 4478 in the browser. Below this, the source is not worth trusting on
# its own and the rendered homepage is folded in beside it.
THIN_SOURCE_CHARS = 600


def with_rendered_homepage(site: SiteFacts, render: Any) -> SiteFacts:
    """Add the rendered homepage to the fact set when the source is thin.

    The crawl still covers up to 25 pages and the render only covers the
    homepage, so this adds rather than replaces: a JavaScript built site gets
    text its checks would otherwise never see, and an ordinary site is left
    exactly as it was.
    """
    if render is None or not getattr(render, "usable", False):
        return site
    rendered_text = (getattr(render, "text", "") or "").strip()
    if not rendered_text:
        return site
    if len(site.text.strip()) >= THIN_SOURCE_CHARS:
        return site
    if len(rendered_text) <= len(site.text.strip()):
        return site

    home_path = site.homepage.path if site.homepage else "/"
    rendered_page = PageFacts(
        url=getattr(render, "final_url", None) or getattr(render, "url", "") or "",
        path=home_path,
        title=getattr(render, "title", "") or "",
        text=re.sub(r"\s+", " ", rendered_text).strip().lower(),
        html=(getattr(render, "html", "") or "").lower(),
        rendered=True,
    )
    pages = (*site.pages, rendered_page)
    return SiteFacts(
        homepage=site.homepage or rendered_page,
        pages=pages,
        sitemap_paths=site.sitemap_paths,
        sitemap_lastmod=site.sitemap_lastmod,
        reachable=site.reachable,
        robots_blocked=site.robots_blocked,
    )


def jsonld_types(block: Any) -> set[str]:
    """@type, normalized. Handles a string, a list, and a namespaced URL."""
    if not isinstance(block, dict):
        return set()
    raw = block.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    out: set[str] = set()
    for value in values:
        if isinstance(value, str):
            out.add(value.rsplit("/", 1)[-1].rsplit("#", 1)[-1].strip().lower())
    return out


_TAG = re.compile(r"<[^>]+>")


def strip_tags(html: str) -> str:
    return _TAG.sub(" ", html)
