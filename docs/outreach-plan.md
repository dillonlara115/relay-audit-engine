# Outreach: the plan through sending
### Companion to `engine-spec.md` §9. Decisions recorded, not proposals.

The audit ends at a published report. Getting that report in front of the person
it is about, and learning whether it worked, is a separate arc with its own
rules. This document records what has been built, what comes next, and the one
decision that requires amending a hard rule.

Criteria doc §6 and §7 govern. Where this disagrees with them, they win.

---

## Where this sits

| Phase | What | State |
|---|---|---|
| 1A | Contact discovery and DNS verification | **Built.** 50% coverage on a live Colorado Springs sweep |
| 1B | The four-touch ledger, hand-logged | **Built.** Cadence, parking, rewind, reply policy table |
| Findings pool | Six ranked, three chosen, the rest held for follow-ups | **Built.** Section 6 now has material to send |
| 1C | Reply ingestion and intent classification | **Built.** Gmail read only, six intents, auto suppression on a no |
| 1D | Outcome loop: reply rate by segment and intent | **Built.** `python -m app.cli outcomes` |
| 2 | Console: CSV export, excluded tab, score history | Independent of the rest |
| 3 | Sending, with open and click tracking | **Blocked on an entry condition.** See below |

---

## Phase 3: sending

### The entry condition is not a date

Criteria §7: *"No automated sending until at least thirty have been hand-sent and
a reply rate is known. Automating an unproven message scales a no."*

That number is now computable. The 1B ledger records every touch, and 1C will
record every reply, so `python -m app.cli outcomes` reports both the count and
the rate. Phase 3 does not begin because the calendar says so. It begins when
that command prints thirty and a rate somebody is willing to defend.

When it does, record the date and the rate here, and amend hard rule 4 in
`CLAUDE.md` in the same commit. The rule should be superseded deliberately and
visibly, not eroded.

### Mail leaves the operator's own mailbox, not a sending service

**Decision: OAuth into Google Workspace. Not Resend, not Elastic Email, not
SendGrid, not SES.**

The reasoning is not preference. It is that an email service provider cannot see
the replies, and the replies are the entire point of the outcome loop.

Inbound on every ESP works the same way: you point an MX record at them. Both
Resend and Elastic Email support this properly. But `relayforroofers.com` already
MXes to Google Workspace and a domain has one set of MX records. The workaround
is a subdomain, `reply.relayforroofers.com`, MXed to the provider, with a
`Reply-To` header set on every message. That works technically and costs us the
thing the outreach is built on: a roofer receiving a first cold email sees a
reply address that does not match the person who wrote to him.

Sending from the operator's own mailbox has none of that. Mail goes from the real
address over the domain's existing SPF, DKIM and DMARC. Replies come back to the
same mailbox. The same OAuth grant that sends can read them, so reply tracking
needs no DNS change at all.

This is not a novel conclusion. Swokei, whose entire product is cold outreach for
agencies, is built exactly this way: OAuth into Gmail or Microsoft 365, or raw
SMTP credentials. Their own sending page names Postmark, SendGrid and Amazon SES
as providers *you bring*, not services they run, and leads with "Your domain.
Your reputation. Your replies." A company that sells nothing but outreach did not
build it on an ESP, and the reply path is why.

It also explains their pricing, which is worth understanding before copying any
of it. Credits meter site analysis and address verification only; sending is
unlimited on every paid plan. That is not generosity. Their marginal cost per
email is zero because Google and Microsoft carry delivery, so the model call is
the only thing they can meter. The corollary is that deliverability risk sits on
the customer's domain rather than theirs.

### Read before write, and let Google enforce it

**1C requests `gmail.readonly` and nothing more. Phase 3 widens the scope.**

Built as specified. `app/tools/gmail.py` refuses to construct a client from a
token carrying any other scope, and `gmail-connect` refuses to store one, so a
credential that can send cannot enter the system even by accident. Three other
properties are enforced there rather than intended: search is always scoped to
addresses already held and an empty address list makes no API call at all, so
there is no code path that lists a mailbox; and only an excerpt is stored,
capped, with quoted history and signatures stripped before anything is written
down.

Reading replies transmits nothing and does not touch rule 4. Requesting only the
read scope makes that structural rather than aspirational: a token that cannot
send means no code path can send, including code nobody has written yet.

`tests/test_console.py::test_nothing_in_the_app_can_transmit_mail` greps the
package for `smtplib`, `sendgrid`, `resend`, `boto3` and six more. It is a good
guard and it is still a string match written by hand. An OAuth scope is enforced
by Google.

Widening it at Phase 3 means a new consent screen the operator has to click.
That is a better place for the decision to live than a line in a config file.

### What to take from how Swokei does it

**A deliverability check on connect.** They verify SPF, DKIM and DMARC alignment
for the sending domain the moment a mailbox is attached, and warn on anything
misconfigured. It sends nothing, costs nothing, and is worth running before the
first hand-sent email rather than after the thirtieth.

**Per-mailbox daily caps and a send window.** Pacing so a batch reads like a
person working a list. Our volume is low enough that this is close to free, but
the control should exist before it is needed rather than after a morning when
forty went out at once.

**Auto-pause on reply.** Already built: `app/outreach.py` closes or parks a
sequence on every intent that warrants it, so a prospect who has answered cannot
receive the next scheduled touch.

### What to refuse, and why

**Multi-mailbox rotation.** Swokei describes it as spreading volume "so no single
mailbox sees suspicious activity." That feature exists because providers throttle
and flag bulk unsolicited mail, and the answer it offers is to spread across
enough accounts that none individually trips detection. It solves a problem we
have chosen not to have.

**Warm-up pools and unlimited caps.** Same reasoning. Criteria §7 sets target
volume at roughly one hundred a month and says plainly: *"This method is
specificity at low volume. If tooling tempts you toward five hundred with thinner
evidence, it has made things worse."* Rotation is that temptation with a UI.

**Generic fallback copy.** Swokei will write a lead an email when its site is
unreachable or absent. We will not. Every claim traces to a stored artifact, per
the §10 pre-send checklist, and a message with no evidence behind it is the one
thing the whole audit exists to avoid.

### Tracking

Pixel and rewritten links, per recipient, with the pixel toggleable.

**Hash every IP with `REPORT_IP_SALT` before it is stored.** Hard rule 6 already
requires this and the public report path already does it, so the pattern carries
over directly rather than needing invention.

**Open rate is a soft number and should be labelled as one in the console.**
Apple Mail Privacy Protection pre-fetches images, which inflates opens for a
large share of consumer mail, and roofing owners skew heavily to iPhone. Swokei
offers a toggle to skip the pixel entirely, which is a quiet admission of the
same thing. Click and reply are the signals worth acting on, and §7's threshold
is written in terms of reply rate, so that is the number the outcome loop ranks
findings by.

### Still open

- Throttle and daily cap values. Cannot be chosen sensibly until the hand-sent
  thirty show what a normal day looks like.
- Whether each send still needs individual human approval, or whether approving
  the findings (rule 7) is approval enough for the message built from them. This
  is the rule 7 question restated for a channel that did not exist when it was
  written, and it should be answered before the first automated send, not after.
- Whether a suppression triggered by a reply should also suppress the domain, or
  only the address that replied. Today `not_interested` suppresses on reply; the
  match breadth is worth deciding deliberately.

---

## Reference: what 1A and 1B already decided

Recorded here because the reasoning is otherwise only in commit messages.

**Contacts are discovered from the gate crawl**, which already fetches six pages
per prospect. No additional requests, so the §8 politeness budget is untouched.
Addresses rank by what a person can do with them: a named individual at the
company's own domain over a shared mailbox over a free consumer account.

**Verification is DNS only.** MX with an A record fallback, because small
contractors are frequently configured that way and the RFC delivers to them. A
lookup that could not be performed is `unknown`, never `invalid`, on the same
principle that an unrun check never counts as a failure.

**Manual contacts survive re-crawl.** An address a person hunted down after a
`wrong_person` reply outranks anything on the site and is held in a separate
field discovery never writes to.

**Findings are drafted six at a time and chosen three.** The model ranks up to
six failures worst first; a person picks the three the report carries; the rest
become follow-up material, one per touch. A pool is capped by how many checks
actually failed, so a site with four failures yields two touches rather than
four, and the sequence closes with `no findings left to send` rather than
sending a nudge with nothing new in it.

This also strengthens rule 7 rather than bending it. Approving a model's only
three is closer to a rubber stamp than a selection; choosing three from six is
the human act the rule describes. Neither the console nor the CLI has a default
selection, and `approve --yes` with no `--pick` is an error.

**Classification and consequence are separate.** `app/agents/classifier.py`
returns an intent and a confidence and nothing else. `app/outreach.POLICIES`
decides what an intent does. A model never suppresses anybody, and an operator
can correct a label without the damage already being done. Below a confidence
of 0.6, and on any model fault, the reply resolves to `other`, which parks the
sequence for a person: suppressing a prospect on a coin flip is the one
unrecoverable mistake in this path.

**The reply policy table** lives in `app/outreach.py`. Two entries are worth
restating because they are judgements rather than mechanics:

- `maybe_later` closes the sequence and sets a revisit date ninety days out,
  rather than deferring the next touch. Resuming would send touch three of four
  against a quarter-old audit, and naming a problem he has since fixed is worse
  than not writing at all. A revisit re-audits first.
- `out_of_office` and `wrong_person` do not burn a touch. Neither reached anyone
  who could decide, so neither should count against the four.
