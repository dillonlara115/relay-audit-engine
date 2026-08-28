"""Signal extraction from a crawl. Regex-driven, so the edge cases are the
proof: a match that exists but does not survive plausibility filtering.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.tools.crawl import FetchResult, SiteCrawl
from app.tools.site_signals import extract_signals

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)
HOST = "https://peakroofing.com"


def crawl(html: str) -> SiteCrawl:
    url = f"{HOST}/"
    return SiteCrawl(base_url=HOST, homepage=FetchResult(url=url, final_url=url, status=200, html=html))


def doc(body: str) -> str:
    return f"<html><body>{body}</body></html>"


def test_a_current_copyright_year_is_read():
    signals = extract_signals(crawl(doc("<footer>&copy; 2026 Peak Roofing</footer>")), now=NOW)
    assert signals.copyright_year == 2026


def test_the_newest_of_several_copyright_years_wins():
    html = doc("<footer>Copyright 2019</footer><p>&copy; 2024</p>")
    signals = extract_signals(crawl(html), now=NOW)
    assert signals.copyright_year == 2024


def test_a_copyright_year_outside_the_plausible_range_does_not_crash():
    """Regression: a live sweep of Pueblo crashed the whole batch on this.

    The regex matches any 19xx/20xx year after "copyright" or "(c)", so a site
    whose only match is an old founding year printed with a copyright symbol
    left the unfiltered list non-empty while every value fell outside 1990 to
    now+1. max() on the then-empty filtered generator raised ValueError and
    took the sweep down with it. The fix filters before checking emptiness, so
    this case now yields an absent field instead of a crash.
    """
    signals = extract_signals(crawl(doc("<footer>&copy; 1985 Peak Roofing</footer>")), now=NOW)
    assert signals.copyright_year is None


def test_only_an_implausible_year_among_several_still_does_not_crash():
    html = doc("<footer>&copy; 1970</footer><p>Copyright 1888</p>")
    signals = extract_signals(crawl(html), now=NOW)
    assert signals.copyright_year is None


def test_a_year_one_beyond_the_ceiling_is_still_accepted():
    signals = extract_signals(crawl(doc(f"<footer>&copy; {NOW.year + 1}</footer>")), now=NOW)
    assert signals.copyright_year == NOW.year + 1


def test_no_copyright_text_at_all_leaves_the_field_absent():
    signals = extract_signals(crawl(doc("<p>We install roofs.</p>")), now=NOW)
    assert signals.copyright_year is None


def test_founded_year_has_the_same_filter_before_check_shape_and_still_works():
    """The two lines this bug's fix was made to match. Pinned so a future
    edit cannot regress both at once without a test noticing."""
    signals = extract_signals(crawl(doc("<p>Family owned since 2005.</p>")), now=NOW)
    assert signals.founded_year == 2005


def test_years_in_business_with_only_an_implausible_count_is_absent():
    signals = extract_signals(crawl(doc("<p>200 years of experience.</p>")), now=NOW)
    assert signals.years_in_business is None


# ── JavaScript rendered sites ────────────────────────────────────────────────
#
# Measured on a real prospect: its served HTML held 4 characters of visible
# text across five crawled pages, while the browser rendered 4478. Every text
# based check was reading the empty version and failing by default.


class _Render:
    def __init__(self, text="", html="", usable=True, title="", url="https://x.com/"):
        self.text, self.html, self.usable = text, html, usable
        self.title, self.url, self.final_url = title, url, url


def _facts(html: str):
    from app.checks.extract import build

    return build(crawl(html))


def test_a_thin_source_gets_the_rendered_homepage_folded_in():
    from app.checks.extract import with_rendered_homepage

    site = _facts(doc("<div id='root'></div>"))
    assert len(site.text.strip()) < 50, "the source really is nearly empty"

    merged = with_rendered_homepage(site, _Render(
        text="Licensed and insured. 10 year workmanship warranty. Financing available.",
        html="<script src='https://embed.tawk.to/x'></script>",
    ))
    assert "licensed and insured" in merged.text
    assert "workmanship warranty" in merged.text
    assert "tawk.to" in merged.html
    assert merged.pages[-1].rendered is True


def test_a_healthy_source_is_left_completely_alone():
    """An ordinary server rendered site must not gain a duplicate page."""
    from app.checks.extract import with_rendered_homepage

    body = "<p>" + ("We install and repair residential roofs across the region. " * 20) + "</p>"
    site = _facts(doc(body))
    merged = with_rendered_homepage(site, _Render(text="something else entirely"))
    assert merged is site
    assert not any(p.rendered for p in merged.pages)


def test_an_unusable_render_changes_nothing():
    from app.checks.extract import with_rendered_homepage

    site = _facts(doc("<div id='root'></div>"))
    assert with_rendered_homepage(site, _Render(text="x", usable=False)) is site
    assert with_rendered_homepage(site, None) is site


def test_a_render_with_no_text_changes_nothing():
    from app.checks.extract import with_rendered_homepage

    site = _facts(doc("<div id='root'></div>"))
    assert with_rendered_homepage(site, _Render(text="   ")) is site


def test_the_rendered_page_keeps_the_homepage_path_so_page_counts_hold():
    """F10 counts distinct paths. The rendered copy must not invent a new one."""
    from app.checks.extract import with_rendered_homepage

    site = _facts(doc("<div id='root'></div>"))
    merged = with_rendered_homepage(site, _Render(text="Licensed and insured roofers here."))
    assert merged.pages[-1].path == "/"
    assert merged.all_paths.count("/") == 1, "deduped, not doubled"
