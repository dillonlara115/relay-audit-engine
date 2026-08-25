# The Found-to-Booked Audit
### Prospect scoring spec v2.0 — Relay for Roofers

Changed in v2: the Booked section no longer depends on a lead-form probe. Everything
here is observable from outside the business, which means the whole audit can run
unattended across a market. The probe survives as a paid diagnostic, not as an
ingest-time check. See section 9.

The audit is evidence, not opinion. Every check has a defined threshold, a data
source, and a note on whether a machine can run it. Nothing here is shown to a
roofer in this form. He sees findings about his business, in his language, in the
report structure at the end.

| Section | Question it answers | Weight |
|---|---|---|
| **Found** | Can a homeowner with a leaking roof find him at all? | 30 |
| **Chosen** | Once found, does he look like the safe choice? | 30 |
| **Booked** | If someone raises a hand, is anything set up to catch it? | 40 |

Booked still carries the most weight. The evidence is weaker than a measured
response time, but it is still the section nobody else audits, and it is still the
section we are built to fix.

---

## 0. Fit gate (pass/fail, runs first)

If a prospect fails the gate, no audit runs. This is the "broke client" screen.

| Gate check | Pass condition | Source | Auto |
|---|---|---|---|
| Residential work | Residential roofing in services or GBP category | GBP + site | Yes |
| Commercial-only | Not commercial-exclusive | Site copy | Yes |
| Revenue proxy A | 25+ Google reviews | Places API | Yes |
| Revenue proxy B | 5+ years in business | Site / GBP | Partial |
| Revenue proxy C | Careers page, named crew, fleet photos, or office address | Site | Partial |
| Real local operator | Local address in metro, local phone | GBP | Yes |
| Not a storm chaser | Photos or reviews spanning 24+ months | Places API | Yes |
| Territory clear | No overlap with an active client's area | Internal | Yes |
| Reachable owner | Owner or GM name findable | GBP / site | Partial |

Result: PASS / FAIL / REVIEW. Only PASS and REVIEW continue.

**Burned Skeptic flag.** If an incumbent agency footprint is detected (a known
vendor's tracking script, a templated vendor site, a vendor-branded chat widget),
tag `incumbent_agency`. Wedge persona. The outreach opener changes for them.

---

## 1. FOUND — 30 points

| # | Check | Full credit | Pts | Source |
|---|---|---|---|---|
| F1 | GBP claimed and verified | Claimed | 2 | Places |
| F2 | Primary category | Roofing Contractor | 1 | Places |
| F3 | Review count | 50+ | 3 | Places |
| F4 | Average rating | 4.5+ | 2 | Places |
| F5 | Review recency | Newest within 30 days | 3 | Places |
| F6 | Photo recency | Newest within 90 days | 1 | Places |
| F7 | **Phone match** | Site phone == GBP phone | 3 | Site + Places |
| F8 | Map pack presence | Top 3 for "roofer [city]" | 3 | SERP |
| F9 | Organic presence | Top 10 for "roof replacement [city]" | 3 | SERP |
| F10 | Service area coverage | 3+ city pages indexed | 2 | Crawl |
| F11 | Content freshness | Any page in last 180 days | 1 | Crawl |
| F12 | Paid search | Running Google Ads | 1 | SERP |
| F13 | Paid social | Active in Meta Ad Library | 1 | Ad Library |
| F14 | **Machine-readable reviews** | Review or AggregateRating schema | 2 | Crawl |
| F15 | Business schema | LocalBusiness with correct NAP | 1 | Crawl |
| F16 | AI answer presence | Named for "best roofer [city]" | 1 | Manual |

F7 and F14 are the invisible-gap checks. Roughly a third of local businesses publish
a phone number that does not match their profile, and close to nine in ten carry
none of their reviews on their own site in machine-readable form. Cheap to verify,
lands hard, and the owner has no idea.

---

## 2. CHOSEN — 30 points

| # | Check | Full credit | Pts | Source |
|---|---|---|---|---|
| C1 | Site loads | 200 on https | 2 | Fetch |
| C2 | Mobile usable | No horizontal scroll, 16px+ text | 2 | Render 390px |
| C3 | Mobile speed | PSI mobile 60+ | 2 | PSI |
| C4 | LCP | Under 2.5s mobile | 2 | PSI |
| C5 | Phone above fold | Visible without scrolling | 3 | Render |
| C6 | Click-to-call | `tel:` on the header number | 2 | Crawl |
| C7 | Form above fold | Or one visible primary CTA | 2 | Render |
| C8 | Form friction | 5 fields or fewer | 2 | Crawl |
| C9 | Real project photos | Own photos, not stock | 2 | Vision |
| C10 | Reviews on page | Testimonials on home | 2 | Crawl |
| C11 | Licensed / insured | Stated on site | 1 | Crawl |
| C12 | Manufacturer credential | GAF, Owens Corning, CertainTeed tier | 2 | Crawl |
| C13 | Warranty terms | Workmanship warranty stated | 1 | Crawl |
| C14 | Financing | Mentioned or applied for on site | 1 | Crawl |
| C15 | Insurance / storm page | Dedicated claim-help page | 2 | Crawl |
| C16 | Footer copyright | Current or last year | 1 | Crawl |
| C17 | Trust read | Vision verdict of adequate or better | 1 | Vision |

Front Range note: C15 carries real weight. Hail claims are the buying trigger for a
large share of Colorado replacements, and a roofer with no claim-help page is
invisible at the moment a homeowner is most motivated.

---

## 3. BOOKED — 40 points

We cannot see how fast his team moves. We can see whether anything is set up to
catch a lead at all, and the answer is usually no.

| # | Check | Full credit | Pts | Source |
|---|---|---|---|---|
| B1 | **Self-serve booking** | Homeowner can pick a time without waiting | 10 | Crawl |
| B2 | **Form health** | Form has an action, validates, resolves without error | 8 | Render |
| B3 | Missed-call text-back | Detected on the primary number | 6 | Crawl |
| B4 | Live chat | Widget present and configured | 4 | Crawl |
| B5 | Response promise | A stated response time anywhere on site | 4 | Crawl |
| B6 | Confirmation clarity | Thank-you state tells him what happens next | 4 | Render |
| B7 | After-hours coverage | Hours published and an after-hours path stated | 4 | Crawl + Places |

**B2 is the sleeper.** A silently broken contact form is the most expensive defect a
roofing site can have, it is invisible to the owner because nothing errors on his
end, and it is trivial for us to detect. Fill the form with the audit identity,
verify required-field validation behaves, verify the action resolves.
**Never submit it.** See section 8.

### Honest framing in the report

The report says what we measured and no more. Approved language:

> From the outside we can see whether the tools are in place to catch a lead. We
> cannot see how fast your team actually moves. That is the next thing worth
> measuring.

That sentence is not a hedge. It is the setup for the paid diagnostic.

### Measurement layer (diagnostic only, 0 pts, always recorded)

| Check | Source |
|---|---|
| GA4 installed | Crawl |
| Google Ads conversion tag | Crawl |
| Meta pixel present while running Meta ads | Crawl + Ad Library |
| Call tracking number in use | Crawl |

The pixel gap is the loudest. Something over eight in ten local advertisers running
Meta ads have no detectable pixel, meaning they pay for traffic the platform cannot
learn from. Only evaluate it when F13 confirmed active ads. A missing pixel on a
business not running ads is not a finding.

---

## 4. Scoring, grading, segments

**Total: 100.**

| Score | Band | Meaning |
|---|---|---|
| 85–100 | Dialed | Well run. Referral partner, not a prospect. |
| 65–84 | Tuned | Real gaps, no crisis. Longer sale. |
| 40–64 | Leaking | Best prospects. Clear before and after. |
| Under 40 | Broken | Big project, slow sale, verify budget twice. |

Score alone is not the routing decision. The **shape** is.

| Segment | Signature | Priority | Opening angle |
|---|---|---|---|
| **Leaky Bucket** | Found 20+/30, Booked 20-/40 | **1** | Already pays for demand, nothing catches it. Our wedge, shortest sale, budget exists. |
| **Invisible Pro** | Found 15-/30, Booked 28+/40 | 2 | Set up to convert, nobody finds him. Visibility sale. |
| **Both Broken** | Both low, gate passed | 3 | Full rebuild. Real money, slow close. |
| **Dialed** | Both high | 4 | Ask who else he knows. |

Leaky Bucket drives the batch. A roofer spending on ads with no booking path and a
broken form is the best conversation available, and his total score will often be
mid-range, which is why score-ranking alone would bury him.

---

## 5. Leak math (no invented numbers)

The report never states a dollar figure we made up.

Measured and reported: which catch mechanisms exist, which are missing, whether the
form works.

Supplied by him at `/tools/lead-leakage-calculator/`: inquiries per month, average
job value, current close rate.

He does the arithmetic. Per the value conversation, the number has to come out of
his mouth.

---

## 6. Report structure

One page, his name at the top, outcome language only, no mechanism named.

1. **What we did.** Two sentences. Searched for a roofer in his city, then looked at
   what a homeowner would find.
2. **What we found.** Evidence with screenshots. No adjectives.
3. **Three things costing him booked jobs.** Ranked by revenue impact, not by ease
   for us. Each: what we saw, what it means to him, what fixing it takes. Never more
   than three.
4. **What good looks like.** One competitor doing it better, named only if the
   comparison is fair and factual.
5. **His number.** Link to the calculator.
6. **One ask.** Reply if he wants it applied. He keeps the findings either way, no
   strings. No calendar link, no deck, no agency intro.

Follow-ups add one new finding each: day 3, day 7, day 14, then stop. Four touches
and silence is a no.

---

## 7. Outreach rules

- **Suppression is checked before every outreach action**, including draft
  generation. Match on place_id, domain, phone, and email.
- Any request not to be contacted is permanent and immediate.
- No automated sending until at least thirty have been hand-sent and a reply rate is
  known. Automating an unproven message scales a no.
- Target volume is roughly one hundred per month. This method is specificity at low
  volume. If tooling tempts you toward five hundred with thinner evidence, it has
  made things worse.

---

## 8. Crawl conduct

- Respect `robots.txt`. Disallowed means the check is skipped and noted, never
  bypassed.
- Identify honestly: `RelayAuditBot/1.0 (+https://relayforroofers.com/bot)`.
- 2 requests per second per host, 25 pages maximum, 15 second timeout.
- **Never submit a form.** Filling to test validation is fine. Submitting sends a
  real person a real notification.

---

## 9. The probe (removed from the audit, retained as a product)

Measuring actual speed-to-lead requires submitting a real inquiry and timing the
response. That is mystery shopping. It is legitimate at low volume with a real
identity, but it does not belong in an unattended pipeline and it does not belong in
anything public.

It moves to the paid diagnostic rung, run by hand, for clients and for prospects who
have asked for it. Rules of engagement when it runs:

1. Real identity. Real name, real mobile, real address in the service area.
2. Real intent, honestly stated if asked.
3. One probe per business, ever. Cancel any scheduled inspection immediately.
4. Log timestamps. The timestamp is the entire deliverable.
5. Any opt-out request goes to permanent suppression.

For a client like Donovan or Danny this is not a favor to ask. It is the service.

---

## 10. Pre-send checklist

- [ ] Fit gate passed
- [ ] Not on the suppression list
- [ ] Every claim traceable to a stored artifact
- [ ] Zero invented numbers
- [ ] No mechanism named
- [ ] No em-dashes
- [ ] Three findings maximum
- [ ] Booked framing states the limit of what we measured
- [ ] Territory does not conflict with an active client
- [ ] Comfortable if he forwarded this to the competitor we named
