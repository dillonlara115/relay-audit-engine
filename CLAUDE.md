# CLAUDE.md

Project instructions for Claude Code. Read this before writing anything.

---

## What we are building

`relay-audit-engine` is an internal prospecting system for Relay for Roofers, a
marketing agency serving residential roofing contractors in the Western U.S. and the
Colorado Front Range.

It ingests a metro, screens a hundred roofing contractors for fit, audits every
survivor across forty checks, scores and segments them, and hands back a ranked call
list with a shareable one-page report per prospect.

It is also the submission for the **All Things Agentic Hackathon** (Devpost, Google),
track **Taskmaster**, deadline **Mon Aug 31, 5:00pm PDT** (6:00pm MDT local). Today is
Tue Aug 25.

These are not two products. The hackathon entry is the real tool, built on a
deadline. Do not build anything for the demo that we would not keep.

## Read these first

- `docs/engine-spec.md` is the technical contract. Architecture, data model,
  pipeline, guardrails.
- `docs/found-to-booked-audit-spec.md` v2 is the criteria. What is measured, the
  thresholds, the scoring and segment rules, the report structure.
- Where they disagree: criteria doc wins on what is measured, engine spec wins on
  how it is built.

Anything marked `[after Aug 31]` in the engine spec is out of scope this week.

## Stack (non-negotiable)

| Layer | Choice |
|---|---|
| Models | Gemini 3.5 or newer via Vertex AI |
| Agent framework | Google ADK (Python) |
| Runtime | Cloud Run, two services |
| Data | Firestore |
| Events | Pub/Sub, Cloud Scheduler |
| Storage | Cloud Storage |
| Browser | Playwright in a separate Node service |

The owner's usual stack is SvelteKit, Go, Supabase, and Stripe. **Ignore that here.**
ADK is Python-first and the hackathon requires Google Cloud infrastructure. This
decision is settled. Do not propose porting to Go or Supabase.

Vertex vs Gemini API is one env var, `GOOGLE_GENAI_USE_VERTEXAI`, default TRUE. If
Vertex auth blocks progress for more than an hour, flip to FALSE with an API key and
keep moving. Both satisfy the rules.

## Architecture

```
sweep_coordinator (LlmAgent)
  ├── ingest        tool     Places search, upsert prospects
  ├── gate          tool     fit gate
  └── audit_agent (fanned out per prospect over Pub/Sub)
        ├── recon (SequentialAgent)   robots → homepage → sitemap → key pages
        ├── inspector (ParallelAgent) onpage | speed | vision
        ├── booked        tool        booking, form health, chat, text-back
        ├── score         tool        pure function
        └── diagnostician LlmAgent    picks 3 findings, writes consequences
  ├── ranker        tool     sorts by segment priority
  └── opener        LlmAgent drafts one opening line per top prospect
```

**Only four components get a model:** coordinator, vision, diagnostician, opener.
Everything else is a plain function. If you find yourself writing an agent whose only
job is to call PageSpeed and return a number, make it a tool.

The renderer service is stateless. URL in, screenshot and DOM metrics out. It never
touches Firestore or Vertex.

## Hard rules

1. **Never submit a form.** Filling one to test validation is fine and is check B2.
   Submitting sends a real person a real lead notification. There is no exception and
   no flag that enables it. Speed-to-lead probing is a separate manual product, not
   part of this pipeline.
2. **Respect robots.txt.** Disallowed means the check is skipped and noted. Never
   spoof the user agent.
3. **Suppression is checked before every outreach action**, drafts included.
4. **No automated sending.** Drafts only. No email API in the outreach path.
5. **No invented numbers.** Unknown means the field is absent. Not zero, not an
   estimate.
6. **Crawl politely.** 2 req/sec per host, 25 pages max, honest user agent.

## Copy rules (this ships to real contractors)

- Outcome language only. Never name a mechanism, tool, model, or platform in
  anything a contractor reads. "Nobody can book a time without waiting for a call
  back," not "no scheduling widget detected."
- **No em-dashes in user-facing copy.** Add a test that fails the build if one
  appears in report templates or model output.
- Exactly three findings, enforced by runtime assertion.
- Booked findings must state the limit honestly: we can see whether the tools exist,
  not how fast his team moves.
- Brand tokens: asphalt `#16120E`, chalk `#ECE6DC`, safety orange `#F25C1F`.
  Barlow Condensed display, Work Sans body. 16px minimum, mobile first.
- Public reports expose no scores, bands, or segments.

## Working style

- Lead with the answer. Reasoning after, briefly.
- Make sensible assumptions and flag them rather than stalling on questions.
- Surface real choices with tradeoffs. Do not present one option as inevitable.
- Prefer working code today over correct architecture next week. This is a sprint.
- When something is cut for time, say so out loud rather than quietly simplifying.

## Day plan and gates

Do not move on until the gate passes.

| Day | Work | Gate |
|---|---|---|
| Tue 8/26 | Project setup, Firestore model, composite indexes, Places ingest, crawl tool, fit gate | `python -m app.cli sweep "Colorado Springs"` returns 40+ prospects with gate results |
| Wed | Renderer on Cloud Run, PSI, on-page checks, vision agent | Found and Chosen scores with plain-language notes for a real site |
| Thu | Pub/Sub fan-out, per-host rate limiting, retries, dead-letter, resume | A 40-prospect batch completes unattended and survives a killed worker |
| Fri | Booked checks, scoring, segmentation, ranker, report template | A ranked call list with three findings per prospect |
| Sat | Deploy, full 100-prospect sweep on a real metro, capture Cloud Console proof | One completed sweep with evidence |
| Sun | Demo video, architecture diagram, README, write-up | Uploaded to Devpost as a draft |
| Mon | Submit by noon MDT, bonus content, scale to zero | Submitted |

**Thursday is the day this succeeds or fails.** Batch orchestration and resumption is
the least glamorous work and the only part with no fallback. If Wednesday runs long,
cut checks. Never borrow from Thursday.

## Cut list, in order

1. F16 AI answer presence (manual anyway)
2. C9 stock photo detection (keep C17 trust read, it demos better)
3. F13 Meta Ad Library
4. C12 manufacturer credential
5. B4 live chat, B5 response promise
6. The opener agent (rank without drafts)

**Never cut:** the parallel inspector fan, batch resumption, the fit gate, the
segment logic, or the Google Cloud proof shots.

## Never do these

1. Submit a form.
2. Add a check not in the criteria doc.
3. Place a programmatic outbound phone call.
4. Bypass robots.txt.
5. Commit a secret. Secret Manager only. `.env.example` fully keyed, fully blank.
6. Store a raw IP. Hash it with `REPORT_IP_SALT`.
7. Auto-select the three report findings by score rank. A human picks.
8. Propose porting to Go or Supabase.

## Cost control

`gemini-3.5-flash` for every call. Min instances 0, max 2 on both services. Budget
alert at $50. Protect the public Cloud Run URLs, since an open endpoint that triggers
crawls and model calls is a credit drain. Scale to zero after the demo is recorded.

## Submission (Sunday)

- Category, text description, hosted URL if available
- Public repo with spin-up instructions a stranger could follow
- Architecture diagram at `docs/architecture.png`
- Roughly 4-minute demo video proving the backend runs on Google Cloud
- Submit as **Relay for Roofers LLC** from a `@relayforroofers.com` address, which
  makes the entry eligible for the Startup Excellence prize
