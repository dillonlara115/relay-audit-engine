"""Tools on a prospect's site that can make our measurements read wrong.

Two kinds. Speed tools that change what a test sees: NitroPack serves speed
tests an optimised copy of the page, so Lighthouse can look better than what a
visitor gets, and script-delaying tools hold chat and booking widgets back
until someone scrolls or taps, so a quick look can miss them. And robot checks
a host puts in front of the site, which can show our crawler or renderer a
challenge page instead of the site.

Detected from what we already have: the audit's rendered homepage, and the
DataForSEO crawl's own technology read. Nothing extra is fetched. Console only:
these are notes for the operator reading the results, not for the owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    needles: tuple[str, ...]      # lower-case substrings of markup, title or the crawl's tech read
    effect: str
    flatters_speed: bool = False  # lab speed numbers can read better than visitors' experience
    delays_scripts: bool = False  # widgets can be missing from a quick look
    challenge: bool = False       # a robot check sits in front of the site


TOOLS: tuple[Tool, ...] = (
    Tool("nitropack", "NitroPack", ("nitropack", "nitrocdn.com", "nitro-lazy", "window.nitro"),
         "Serves speed tests an optimised copy of the page, so Lighthouse can look faster than "
         "what visitors get; trust the real-visitor timings when Google has them. It also delays "
         "scripts and text, so chat or booking widgets and crawled word counts can come out low.",
         flatters_speed=True, delays_scripts=True),
    Tool("wp_rocket", "WP Rocket", ("wp-rocket", "rocket-lazyload", "data-rocket-", "this website is like a rocket"),
         "Can delay scripts until someone scrolls or taps, which flatters lab speed and can hide "
         "chat or booking widgets from a quick look.", flatters_speed=True, delays_scripts=True),
    Tool("litespeed", "LiteSpeed Cache", ("litespeed-cache", "/litespeed/", "data-lazyloaded", "litespeed"),
         "Can delay scripts until someone interacts, which flatters lab speed and can hide widgets "
         "from a quick look.", flatters_speed=True, delays_scripts=True),
    Tool("perfmatters", "Perfmatters", ("perfmatters",),
         "Can delay scripts until someone interacts, which flatters lab speed and can hide widgets "
         "from a quick look.", flatters_speed=True, delays_scripts=True),
    Tool("rocket_loader", "Cloudflare Rocket Loader", ("rocket-loader.min.js",),
         "Loads scripts late, so widgets can be missing from a quick look.", delays_scripts=True),
    Tool("siteground_check", "SiteGround robot check", ("robot challenge screen", "sgcaptcha"),
         "Shows crawlers a challenge page. Our crawl and checks may have read that page instead "
         "of the site.", challenge=True),
    Tool("cloudflare_check", "Cloudflare browser check", ("just a moment...", "cf-chl-", "challenge-platform",
                                                         "attention required! | cloudflare"),
         "Shows crawlers a challenge page. Our crawl and checks may have read that page instead "
         "of the site.", challenge=True),
    Tool("sucuri_check", "Sucuri firewall", ("sucuri website firewall", "sucuri_cloudproxy", "access denied - sucuri"),
         "Can block or challenge crawlers. Our crawl and checks may have read a block page "
         "instead of the site.", challenge=True),
)
BY_KEY = {t.key: t for t in TOOLS}


def detect(*texts: str | None) -> list[str]:
    """Keys of the tools whose fingerprints appear in any of the texts."""
    hay = " ".join((t or "").lower() for t in texts)
    if not hay.strip():
        return []
    return [t.key for t in TOOLS if any(n in hay for n in t.needles)]


def found_on(audit: Mapping[str, Any]) -> list[Tool]:
    """Everything known for this audit: what the audit's render showed, what
    the crawl reported, and a crawl that had to get past a robot check."""
    keys: list[str] = list((audit.get("stack") or {}).get("tools") or [])
    technical = audit.get("technical") or {}
    keys += detect(technical.get("cms"))
    out = [BY_KEY[k] for k in dict.fromkeys(keys) if k in BY_KEY]
    if technical.get("needs_browser") and not any(t.challenge for t in out):
        out.append(Tool("robot_check", "Robot check", (), BY_KEY["siteground_check"].effect, challenge=True))
    return out


def any_of(tools: Iterable[Tool], attr: str) -> bool:
    return any(getattr(t, attr) for t in tools)
