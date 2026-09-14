"""F8, F9 and F12. Where a homeowner's search actually puts this contractor.

These three are the only checks that look at the search page itself rather than
at his site or his profile, and they are the ones that answer the question the
Found section is named for. Everything else in Found measures whether he could
be found. These measure whether he is.

All three skip when the lookup did not happen. A contractor with no website has
no result to match, and scoring him out of a map pack he may well be sitting in
would be exactly the invented number rule 5 forbids.
"""

from __future__ import annotations

from app.checks.base import AuditContext, CheckResult, check, result, skip
from app.tools.serp import MAP_PACK_TOP, ORGANIC_TOP


def _needs_serp(ctx: AuditContext, code: str) -> CheckResult | None:
    serp = ctx.serp
    if serp is None:
        return skip(code, "No search was run for this business, so this was not checked.")
    if not getattr(serp, "ok", False):
        return skip(code, "The search could not be run, so this was not checked.",
                    error=getattr(serp, "error", None))
    return None


def _city(ctx: AuditContext) -> str:
    return str(ctx.field("city") or "their area")


@check("F8")
def f8_map_pack(ctx: AuditContext) -> CheckResult:
    """Top 3 of the map results for "roofer [city]"."""
    blocked = _needs_serp(ctx, "F8")
    if blocked:
        return blocked

    serp = ctx.serp
    rank = serp.map_pack_rank
    city = _city(ctx)
    if serp.in_map_pack:
        return result("F8", True,
                      f"They come up at number {rank} in the map results when "
                      f"someone searches for a roofer in {city}.",
                      map_pack_rank=rank)
    if rank is not None:
        return result("F8", False,
                      f"They come up at number {rank} in the map results for a "
                      f"roofer in {city}, below the top {MAP_PACK_TOP} a homeowner "
                      f"sees without tapping through.",
                      map_pack_rank=rank)
    return result("F8", False,
                  f"They do not come up in the map results when someone searches "
                  f"for a roofer in {city}.",
                  map_pack_rank=None)


@check("F9")
def f9_organic(ctx: AuditContext) -> CheckResult:
    """Top 10 of the ordinary results for "roof replacement [city]"."""
    blocked = _needs_serp(ctx, "F9")
    if blocked:
        return blocked

    serp = ctx.serp
    rank = serp.organic_rank
    city = _city(ctx)
    if serp.in_organic:
        return result("F9", True,
                      f"Their website comes up at number {rank} for roof "
                      f"replacement in {city}.",
                      organic_rank=rank)
    if rank is not None:
        return result("F9", False,
                      f"Their website comes up at number {rank} for roof "
                      f"replacement in {city}, past the first page most "
                      f"homeowners ever look at.",
                      organic_rank=rank)
    return result("F9", False,
                  f"Their website does not come up in the first {ORGANIC_TOP} "
                  f"results for roof replacement in {city}.",
                  organic_rank=None)


@check("F12")
def f12_paid_search(ctx: AuditContext) -> CheckResult:
    """Whether an ad of theirs ran on either search we made.

    Two searches cannot prove a contractor buys no ads at all, so a miss is
    reported as what it is: no ad on these two terms. The distinction matters,
    because an owner who is advertising on terms we did not search would read
    the flat claim as proof we never looked.
    """
    blocked = _needs_serp(ctx, "F12")
    if blocked:
        return blocked

    serp = ctx.serp
    city = _city(ctx)
    if serp.paid:
        return result("F12", True,
                      f"They are paying to show up at the top of roofing searches "
                      f"in {city}.",
                      paid=True)
    return result("F12", False,
                  f"No ad of theirs ran on either roofing search we made in "
                  f"{city}. They may still be advertising on other searches.",
                  paid=False, queries=list(getattr(serp, "queries", ()) or ()))
