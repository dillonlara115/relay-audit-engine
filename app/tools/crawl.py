"""Polite crawler. Criteria doc section 8 is the contract this file implements.

- robots.txt is respected. Disallowed means skipped and noted, never bypassed.
- Honest user agent. No spoofing, ever.
- 2 requests per second per host, 2 concurrent per host, 25 pages max, 15s timeout.
- Read only. This module has no code path that issues a POST to a prospect host.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from app.config import get_config

# Prioritized paths, in the order the engine spec lists them. A page whose path
# contains one of these fragments is crawled before generic discovered links.
PRIORITY_FRAGMENTS: tuple[str, ...] = (
    "roof-replacement",
    "roof-repair",
    "repair",
    "storm",
    "hail",
    "insurance",
    "claim",
    "financing",
    "about",
    "review",
    "testimonial",
    "contact",
    "service-area",
    "areas-we-serve",
    "locations",
    "careers",
    "jobs",
    "team",
    "gallery",
    "warranty",
)

_SKIP_SUFFIXES = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".zip", ".mp4", ".mov", ".webm", ".css", ".js", ".xml", ".json",
)

# The gate asks a different question than the audit does. The audit wants the
# money pages; the gate wants proof this is a real company with a history and a
# name on the door. On a five page budget those two orders disagree completely,
# so the gate passes its own.
GATE_PRIORITY_FRAGMENTS: tuple[str, ...] = (
    "about",
    "our-story",
    "who-we-are",
    "team",
    "staff",
    "crew",
    "careers",
    "jobs",
    "employment",
    "hiring",
    "contact",
    "why-us",
    "why-choose",
    "review",
    "testimonial",
)



_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.I | re.S)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)


def parse_w3c_date(raw: str | None) -> datetime | None:
    """Sitemap lastmod, which is a date or a full timestamp. None when unusable."""
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# Places hands back the website with Google's own campaign tags attached, and
# links on a site carry their own. Carrying them through has three costs: the
# provider cache keys on the full URL and re-spends quota on the same page, the
# evidence URL in a report shows a contractor a tracked link, and CrUX field
# data is keyed per URL so a tagged URL never matches the real-user record for
# the page.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "twclid", "ttclid",
    "mc_cid", "mc_eid", "_ga", "_gl", "yclid", "igshid", "si", "ref", "ref_src",
    "hsa_acc", "hsa_cam", "hsa_grp", "hsa_ad", "hsa_src", "hsa_tgt", "hsa_kw",
    "hsa_mt", "hsa_net", "hsa_ver", "campaignid", "adgroupid",
})


def strip_tracking(query: str) -> str:
    """Drop campaign tags, keep everything else.

    Only known tracking keys are removed. A query string can be load bearing,
    and dropping ?page=2 would silently change which page we audited.
    """
    if not query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    return urlencode(kept)


def normalize_url(url: str, *, base: str | None = None) -> str | None:
    """Absolutize, strip fragments and tracking noise. None means unusable."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:", "sms:", "#")):
        return None
    if base:
        url = urljoin(base, url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    path = parsed.path or "/"
    return urlunparse(
        (parsed.scheme, parsed.netloc.lower(), path, "", strip_tracking(parsed.query), "")
    )


def registrable_host(url: str) -> str:
    """Host without a leading www. Good enough for same-site comparison."""
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    html: str | None = None
    content_type: str | None = None
    elapsed_ms: int | None = None
    blocked_by_robots: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and bool(self.html)

    @property
    def is_https(self) -> bool:
        target = self.final_url or self.url
        return urlparse(target).scheme == "https"


@dataclass
class SiteCrawl:
    base_url: str
    homepage: FetchResult
    pages: dict[str, FetchResult] = field(default_factory=dict)
    sitemap_urls: list[str] = field(default_factory=list)
    sitemap_lastmod: dict[str, datetime] = field(default_factory=dict)
    robots_present: bool = False
    robots_blocked: list[str] = field(default_factory=list)
    crawl_delay_seconds: float | None = None

    @property
    def reachable(self) -> bool:
        return self.homepage.ok

    def all_pages(self) -> list[FetchResult]:
        out = [self.homepage] if self.homepage.ok else []
        out.extend(p for p in self.pages.values() if p.ok)
        return out

    def paths(self) -> list[str]:
        return sorted({urlparse(p.final_url or p.url).path for p in self.all_pages()})


class _HostGovernor:
    """One per host. Caps concurrency and enforces a minimum request interval."""

    def __init__(self, concurrency: int, min_interval: float) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def __aenter__(self) -> "_HostGovernor":
        await self._sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()


def text_of(html: str | None) -> str:
    """Visible text, lowercased, whitespace collapsed. Empty string when unusable."""
    if not html:
        return ""
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, template"):
        tag.decompose()
    body = tree.body or tree.root
    if body is None:
        return ""
    return re.sub(r"\s+", " ", body.text(separator=" ")).strip().lower()


def links_of(html: str | None, base_url: str) -> list[str]:
    if not html:
        return []
    out: list[str] = []
    for node in HTMLParser(html).css("a[href]"):
        normalized = normalize_url(node.attributes.get("href") or "", base=base_url)
        if normalized:
            out.append(normalized)
    return out


def _priority_rank(url: str, fragments: Sequence[str] = PRIORITY_FRAGMENTS) -> int:
    path = urlparse(url).path.lower()
    for index, fragment in enumerate(fragments):
        if fragment in path:
            return index
    return len(fragments)


class Crawler:
    """Shared across a whole sweep so per-host limits actually bind."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        cfg = get_config()
        self._cfg = cfg
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(cfg.crawl_timeout_seconds),
            headers={
                "User-Agent": cfg.crawl_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._governors: dict[str, _HostGovernor] = {}
        self._robots: dict[str, tuple[RobotFileParser | None, bool, float | None]] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "Crawler":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    def _governor(self, host: str, crawl_delay: float | None = None) -> _HostGovernor:
        gov = self._governors.get(host)
        if gov is None:
            interval = 1.0 / max(self._cfg.crawl_requests_per_second, 0.1)
            if crawl_delay:
                interval = max(interval, crawl_delay)
            gov = _HostGovernor(self._cfg.crawl_concurrency_per_host, interval)
            self._governors[host] = gov
        return gov

    async def _robots_for(self, url: str) -> tuple[RobotFileParser | None, bool, float | None]:
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        if host_key in self._robots:
            return self._robots[host_key]

        lock = self._robots_locks.setdefault(host_key, asyncio.Lock())
        async with lock:
            if host_key in self._robots:
                return self._robots[host_key]
            parser: RobotFileParser | None = None
            present = False
            delay: float | None = None
            try:
                async with self._governor(parsed.netloc):
                    resp = await self._client.get(f"{host_key}/robots.txt")
                if resp.status_code == 200 and resp.text.strip():
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                    present = True
                    raw_delay = parser.crawl_delay(self._cfg.crawl_user_agent)
                    delay = float(raw_delay) if raw_delay else None
            except httpx.HTTPError:
                # Unreachable robots.txt is not consent to ignore it, but it is
                # also not a disallow. Treat as absent and stay inside our own
                # rate limit.
                parser = None
            self._robots[host_key] = (parser, present, delay)
            return self._robots[host_key]

    async def allowed(self, url: str) -> bool:
        parser, _present, _delay = await self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self._cfg.crawl_user_agent, url)

    async def fetch(self, url: str, *, check_robots: bool = True) -> FetchResult:
        normalized = normalize_url(url)
        if not normalized:
            return FetchResult(url=url, error="unusable url")

        parsed = urlparse(normalized)
        crawl_delay = None
        if check_robots:
            parser, _present, crawl_delay = await self._robots_for(normalized)
            if parser is not None and not parser.can_fetch(self._cfg.crawl_user_agent, normalized):
                return FetchResult(url=normalized, blocked_by_robots=True)

        started = time.monotonic()
        try:
            async with self._governor(parsed.netloc, crawl_delay):
                resp = await self._client.get(normalized)
        # ValueError and InvalidURL cover hosts that only exist in broken
        # markup, like a literal template placeholder in an href. Seen once in
        # the wild as "'cta' does not appear to be an IPv4 or IPv6 address"
        # taking down a whole audit. A URL we cannot request is a fact about
        # that URL, not a reason to stop crawling the other 24 pages.
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            return FetchResult(
                url=normalized,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        content_type = resp.headers.get("content-type", "")
        html = resp.text if "html" in content_type or "xml" in content_type else None
        return FetchResult(
            url=normalized,
            final_url=str(resp.url),
            status=resp.status_code,
            html=html,
            content_type=content_type,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _sitemap_urls(
        self, base_url: str, *, limit: int = 200
    ) -> tuple[list[str], dict[str, datetime]]:
        parsed = urlparse(base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        # /sitemap.xml alone misses most of the market. Yoast publishes
        # /sitemap_index.xml and WordPress core publishes /wp-sitemap.xml, and
        # neither is declared in robots.txt on every install.
        candidates = [
            f"{root}/sitemap.xml",
            f"{root}/sitemap_index.xml",
            f"{root}/wp-sitemap.xml",
            f"{root}/sitemap-index.xml",
        ]
        parser, _present, _delay = await self._robots_for(base_url)
        if parser is not None:
            candidates.extend(getattr(parser, "site_maps", None)() or [])

        found: list[str] = []
        lastmod: dict[str, datetime] = {}
        seen_maps: set[str] = set()
        for candidate in candidates:
            if candidate in seen_maps or len(found) >= limit:
                continue
            seen_maps.add(candidate)
            result = await self.fetch(candidate)
            if not result.ok or not result.html:
                continue
            is_index = "<sitemapindex" in result.html[:2000].lower()

            # <url> blocks carry lastmod. A sitemap index carries none, so it is
            # read for <loc> only and its children are queued.
            blocks = _URL_BLOCK.findall(result.html)
            pairs: list[tuple[str, str | None]] = []
            if blocks and not is_index:
                for block in blocks:
                    loc_match = _LOC.search(block)
                    if loc_match:
                        mod_match = _LASTMOD.search(block)
                        pairs.append((loc_match.group(1), mod_match.group(1) if mod_match else None))
            else:
                pairs = [(loc, None) for loc in _LOC.findall(result.html)]

            for loc, mod in pairs:
                normalized = normalize_url(loc)
                if not normalized:
                    continue
                if is_index and len(seen_maps) < 8:
                    candidates.append(normalized)
                else:
                    found.append(normalized)
                    parsed_mod = parse_w3c_date(mod)
                    if parsed_mod:
                        lastmod[normalized] = parsed_mod
                if len(found) >= limit:
                    break
        return found, lastmod

    async def crawl_site(
        self,
        base_url: str,
        *,
        max_pages: int | None = None,
        extra_paths: Sequence[str] = (),
        priority_fragments: Sequence[str] = PRIORITY_FRAGMENTS,
        fetch_sitemap: bool = True,
    ) -> SiteCrawl:
        """Recon in spec order: robots, homepage, sitemap, prioritized key pages."""
        budget = max_pages if max_pages is not None else self._cfg.crawl_max_pages
        _parser, robots_present, crawl_delay = await self._robots_for(base_url)

        homepage = await self.fetch(base_url)
        crawl = SiteCrawl(
            base_url=base_url,
            homepage=homepage,
            robots_present=robots_present,
            crawl_delay_seconds=crawl_delay,
        )
        if homepage.blocked_by_robots:
            crawl.robots_blocked.append(base_url)
        if not homepage.ok:
            return crawl

        site_host = registrable_host(homepage.final_url or base_url)
        candidates: list[str] = []
        seen: set[str] = {normalize_url(homepage.final_url or base_url) or base_url}

        def offer(urls: Iterable[str]) -> None:
            for candidate in urls:
                if candidate in seen:
                    continue
                if registrable_host(candidate) != site_host:
                    continue
                if urlparse(candidate).path.lower().endswith(_SKIP_SUFFIXES):
                    continue
                seen.add(candidate)
                candidates.append(candidate)

        offer(normalize_url(p, base=base_url) or "" for p in extra_paths if p)
        offer(links_of(homepage.html, homepage.final_url or base_url))

        if fetch_sitemap:
            crawl.sitemap_urls, crawl.sitemap_lastmod = await self._sitemap_urls(base_url)
            offer(crawl.sitemap_urls)

        candidates.sort(key=lambda url: _priority_rank(url, priority_fragments))
        budget_remaining = max(budget - 1, 0)
        targets = candidates[:budget_remaining]

        results = await asyncio.gather(*(self.fetch(url) for url in targets))
        for result in results:
            if result.blocked_by_robots:
                crawl.robots_blocked.append(result.url)
            crawl.pages[result.url] = result
        return crawl
