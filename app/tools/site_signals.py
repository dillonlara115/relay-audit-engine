"""Turn a crawl into the plain facts the fit gate needs.

Pure over strings. No network, no Firestore. Everything here is a claim we can
point at a substring for, which is what makes the gate auditable later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from app.tools.crawl import SiteCrawl, text_of
from app.tools.phones import extract_phones

# ── Vocabulary ────────────────────────────────────────────────────────────────

RESIDENTIAL_TERMS = (
    "residential roof",
    "residential roofing",
    "homeowner",
    "home owner",
    "your home",
    "home's roof",
    "house roof",
    "shingle replacement",
    "asphalt shingle",
    "roof replacement for your home",
)
COMMERCIAL_TERMS = (
    "commercial roof",
    "commercial roofing",
    "flat roof",
    "tpo",
    "epdm",
    "built-up roofing",
    "industrial roofing",
)
COMMERCIAL_ONLY_TERMS = (
    "commercial roofing only",
    "commercial only",
    "we do not do residential",
    "no residential work",
    "exclusively commercial",
    "commercial projects only",
    "strictly commercial",
)

CAREERS_HINTS = ("career", "careers", "join-our-team", "join our team", "employment", "we are hiring", "we're hiring", "now hiring", "open positions", "job openings")
TEAM_HINTS = ("meet the team", "meet our team", "our team", "meet the crew", "our crew", "our staff", "leadership team", "meet our staff")

OWNER_TITLES = ("owner", "founder", "co-founder", "president", "general manager", "ceo")

# Marketing vendor fingerprints. Presence tags the Burned Skeptic wedge; it is
# not a judgement about the vendor, only that someone else is already there.
INCUMBENT_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "scorpion": ("scorpioncms", "scorpion.co", "cdn.scorpion"),
    "blue_corona": ("bluecorona", "blue corona"),
    "hibu": ("hibu.com", "hibuwebsites"),
    "townsquare_interactive": ("townsquareinteractive", "tsi-sites"),
    "thryv": ("thryv.com", "kickservecdn"),
    "marketing_360": ("marketing360", "madwire"),
    "surefire_local": ("surefirelocal",),
    "leadsnearby": ("leadsnearby",),
    "contractor_webmasters": ("contractorwebmasters",),
    "footbridge_media": ("footbridgemedia",),
    "hook_agency": ("hookagency",),
    "webfx": ("webfx.com", "webfxsites"),
    "podium": ("podium.com", "widget.podium"),
    "birdeye": ("birdeye.com", "birdeye-widget"),
    "nicejob": ("nicejob.com", "nicejob.co"),
    "broadly": ("broadly.com",),
    "signpost": ("signpost.com",),
}

_YEAR_SINCE = re.compile(
    r"(?:since|established(?:\s+in)?|est\.?|serving[^.]{0,40}?since|family[- ]owned since)\s+((?:19|20)\d{2})",
    re.I,
)
_YEARS_COUNT = re.compile(
    r"(\d{1,3})\s*\+?\s*(?:years|yrs)(?:[^.]{0,30}?(?:experience|business|serving|industry|roofing))",
    re.I,
)
_STREET = re.compile(
    r"\d{1,6}\s+[A-Za-z0-9.\-' ]{2,40}\s"
    r"(?:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|way|ct|court|pkwy|parkway|cir|circle|ste|suite|hwy|highway|trl|trail|pl|place)\b"
    r"[.,]?(?:\s*(?:ste|suite|unit|#)\s*[\w\-]+)?",
    re.I,
)
_NAME_AFTER_TITLE = re.compile(
    r"\b(?:owner|founder|co-founder|president|general manager|ceo)\s*[,:/|-]?\s*"
    r"([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z'\-]{1,20}){1,2})"
)
_NAME_BEFORE_TITLE = re.compile(
    r"\b([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z'\-]{1,20}){1,2})\s*[,|-]\s*"
    r"(?:owner|founder|co-founder|president|general manager|ceo)\b",
    re.I,
)
_COPYRIGHT_YEAR = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)


@dataclass
class SiteSignals:
    """Facts about a prospect's website. Absent means unknown, never False-by-default."""

    reachable: bool = False
    https: bool | None = None
    status: int | None = None
    robots_blocked: bool = False

    mentions_residential: bool | None = None
    mentions_commercial: bool | None = None
    commercial_only: bool | None = None

    founded_year: int | None = None
    years_in_business: int | None = None

    has_careers_page: bool | None = None
    has_team_page: bool | None = None
    named_crew: bool | None = None
    office_address: str | None = None

    owner_name: str | None = None
    site_phones: list[str] = field(default_factory=list)
    incumbent_agency: str | None = None

    copyright_year: int | None = None
    paths: list[str] = field(default_factory=list)
    pages_crawled: int = 0

    def evidence_available(self) -> bool:
        return self.reachable and self.pages_crawled > 0


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def _visible_text_original_case(html: str | None) -> str:
    if not html:
        return ""
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, template"):
        tag.decompose()
    body = tree.body or tree.root
    if body is None:
        return ""
    return re.sub(r"\s+", " ", body.text(separator=" ")).strip()


def extract_signals(crawl: SiteCrawl, *, now: datetime | None = None) -> SiteSignals:
    now = now or datetime.now(timezone.utc)
    signals = SiteSignals(
        reachable=crawl.reachable,
        https=crawl.homepage.is_https if crawl.homepage.status else None,
        status=crawl.homepage.status,
        robots_blocked=bool(crawl.robots_blocked),
    )
    pages = crawl.all_pages()
    signals.pages_crawled = len(pages)
    if not pages:
        return signals

    lowered_parts: list[str] = []
    original_parts: list[str] = []
    raw_html_parts: list[str] = []
    for page in pages:
        lowered_parts.append(text_of(page.html))
        original_parts.append(_visible_text_original_case(page.html))
        raw_html_parts.append(page.html or "")

    corpus = " ".join(lowered_parts)
    original = " ".join(original_parts)
    raw_html = " ".join(raw_html_parts).lower()
    signals.paths = crawl.paths()

    # Service mix
    signals.mentions_residential = _contains_any(corpus, RESIDENTIAL_TERMS)
    signals.mentions_commercial = _contains_any(corpus, COMMERCIAL_TERMS)
    signals.commercial_only = bool(
        _contains_any(corpus, COMMERCIAL_ONLY_TERMS)
        or (signals.mentions_commercial and not signals.mentions_residential)
    )

    # Tenure
    years = [int(m.group(1)) for m in _YEAR_SINCE.finditer(corpus)]
    plausible = [y for y in years if 1900 <= y <= now.year]
    if plausible:
        signals.founded_year = min(plausible)
    counts = [int(m.group(1)) for m in _YEARS_COUNT.finditer(corpus)]
    plausible_counts = [c for c in counts if 1 <= c <= 120]
    if plausible_counts:
        signals.years_in_business = max(plausible_counts)

    # Substance
    path_blob = " ".join(signals.paths).lower()
    signals.has_careers_page = _contains_any(path_blob, ("career", "job", "employment", "hiring")) or _contains_any(corpus, CAREERS_HINTS)
    signals.has_team_page = _contains_any(path_blob, ("team", "our-staff", "crew"))
    signals.named_crew = _contains_any(corpus, TEAM_HINTS)

    street = _STREET.search(original)
    if street:
        signals.office_address = re.sub(r"\s+", " ", street.group(0)).strip(" ,.")

    # Reachable owner
    for pattern in (_NAME_AFTER_TITLE, _NAME_BEFORE_TITLE):
        match = pattern.search(original)
        if match:
            candidate = match.group(1).strip()
            if 3 <= len(candidate) <= 40:
                signals.owner_name = candidate
                break

    signals.site_phones = [p.e164 for p in extract_phones(raw_html, corpus)]

    for vendor, fingerprints in INCUMBENT_FINGERPRINTS.items():
        if _contains_any(raw_html, fingerprints):
            signals.incumbent_agency = vendor
            break

    copyright_years = [int(m.group(1)) for m in _COPYRIGHT_YEAR.finditer(corpus)]
    if copyright_years:
        signals.copyright_year = max(y for y in copyright_years if 1990 <= y <= now.year + 1)

    return signals


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host or None
