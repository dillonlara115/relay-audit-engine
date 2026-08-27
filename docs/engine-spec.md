# Relay Audit Engine — Technical Specification
**Repo:** `relay-audit-engine`
**Version:** 2.0 (supersedes the Go and Supabase spec, which is retired)
**Companion:** `found-to-booked-audit-spec.md` for criteria and thresholds

Version 2 consolidates what were two documents. The hackathon build and the private
prospecting engine turned out to be the same product once the probe left the
pipeline, so there is one codebase and one stack.

Where this document and the criteria doc disagree: criteria doc wins on what is
measured, this one wins on how it is built.

---

## 1. What it does

Ingest a metro. Screen a hundred roofing contractors for fit. Audit every survivor
across forty checks. Score, band, and segment them. Rank by segment priority rather
than raw score. Hand back a call list with a drafted opener per prospect and a
shareable one-page report per prospect.

Runs unattended. A full metro sweep takes roughly an hour and touches a hundred
hosts, five external APIs, and a headless browser.

**Sprint scope** is marked throughout. Anything marked `[after Aug 31]` is out of
scope until the hackathon deadline passes.

---

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Models | Gemini 3.5+ via Vertex AI | Hackathon requirement. Service-account auth, no API key in the system. |
| Agent framework | Google ADK (Python) | Hackathon requirement. Python-first. |
| Runtime | Cloud Run, two services | Agent service stays small. Renderer carries the browser. |
| Data | Firestore | Fast to stand up, no provisioning, adequate to ~500 prospects. |
| Events | Pub/Sub, Cloud Scheduler | Batch fan-out and the daily tick. |
| Object storage | Cloud Storage | Screenshots and raw API payloads. |
| Browser | Playwright, Node service | Screenshots, mobile metrics, form health. |

**Why not Supabase.** Cloud Run alone satisfies the hackathon infrastructure rule,
so Postgres would have been legal. It was rejected because a database outside the
project weakens the "runs on Google Cloud" proof, and because a Cloud SQL connector
costs most of a day to wire from Cloud Run. Revisit at roughly 500 prospects, when
Firestore's query model starts to hurt.

**Vertex vs Gemini API** is one environment variable, `GOOGLE_GENAI_USE_VERTEXAI`.
Default TRUE. If Vertex auth blocks progress for more than an hour, flip to FALSE
with an API key. Both satisfy the rules.

---

## 3. Architecture

```
                  ┌───────────────────────────┐
                  │  Operator (CLI / thin UI) │
                  └─────────────┬─────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────┐
      │  Google Cloud project                              │
      │                                                    │
      │   agent service (Cloud Run, Python, ADK)           │
      │     ├── Vertex AI            gemini-3.5-flash      │
      │     ├── Firestore            prospects, audits     │
      │     ├── Cloud Storage        evidence              │
      │     ├── Pub/Sub              batch fan-out         │
      │     └── Cloud Scheduler      daily tick            │
      │                                                    │
      │   renderer (Cloud Run, Node, Playwright)           │
      │     stateless: URL in, screenshot + metrics out    │
      └────────────────────────────────────────────────────┘
                │              │              │
         Places API      PageSpeed API    Prospect sites
                         Ad Library
```

The renderer never touches Firestore or Vertex. URL in, screenshot and DOM metrics
out, no state. That is what lets it scale to zero and keeps the browser dependency
from spreading. Say this in the write-up.

---

## 4. Agent design

```
sweep_coordinator (LlmAgent)
  ├── ingest            tool          Places text search, upsert prospects
  ├── gate              tool          fit gate, pure function over Places + homepage
  └── audit_agent (per prospect, fanned out over Pub/Sub)
        ├── recon (SequentialAgent)   robots → homepage → sitemap → key pages
        ├── inspector (ParallelAgent)
        │     ├── onpage    tool      schema, forms, trust signals, tracking
        │     ├── speed     tool      PageSpeed Insights
        │     ├── serp      tool      map pack + organic  [after Aug 31]
        │     └── vision    LlmAgent  screenshot: stock photos, trust read
        ├── booked        tool        booking, form health, chat, text-back
        ├── score         tool        pure function, fully tested
        └── diagnostician LlmAgent    picks 3 findings, writes consequences
  └── ranker            tool          sorts by segment priority, builds the call list
  └── opener            LlmAgent      drafts one opening line per top prospect
```

**Only four components get a model:** coordinator, vision, diagnostician, opener.
Everything else is a plain function. Do not wrap a deterministic API call in an LLM
call. If an agent's only job is to call PageSpeed and return a number, it is a tool.

### ADK orchestration patterns in use
- `SequentialAgent` for recon, because each step feeds the next
- `ParallelAgent` for the inspector, because the three are independent and I/O bound
- Coordinator delegation for routing and partial-failure recovery

Naming these three explicitly in the submission write-up is free signal.

### Prompt constraints

**Vision.** Structured JSON only, never prose:
```
Given this mobile screenshot of a roofing contractor's homepage, answer as JSON:
{"stock_photos": bool, "stock_reason": str,
 "trust_verdict": "strong"|"adequate"|"weak", "trust_reason": str}
Answer as a homeowner deciding who to trust with a $15,000 roof replacement.
Do not comment on design taste. Comment on whether this looks like a real,
established local company.
```

**Diagnostician.** Its output ships to a real contractor, so the brand rules are
load-bearing: outcome language only, no mechanism named, no em-dashes, exactly three
findings ranked by lost booked jobs, never a number that was not measured.

**Opener.** One sentence referencing one specific finding. No pitch, no calendar
link, no agency introduction. Drafts only. See section 9.

---

## 5. Firestore data model

```
markets/{marketId}
  name, center_lat, center_lng, radius_meters, active

batches/{batchId}
  market_id, label, status, counts: {ingested, gated, audited, failed}
  created_at, completed_at

prospects/{prospectId}
  place_id (unique), business_name, website_url
  site_phone, gbp_phone, address, city, state, lat, lng
  review_count, rating, first_review_at, latest_review_at
  owner_name, owner_email, incumbent_agency
  gate_result, gate_reasons[]
  suppressed, suppressed_reason
  latest_audit_id, created_at, updated_at

audits/{auditId}
  prospect_id, batch_id, status
  scores: {found, chosen, booked, total}
  band, segment, partial
  report_slug (unique, 16-char, null until published)
  published_at, error, started_at, finished_at

audits/{auditId}/checks/{code}
  status: pass|fail|skipped|error
  points_awarded, observed {}, note

audits/{auditId}/evidence/{evidenceId}
  code, kind: screenshot|json|html
  gcs_path, payload, captured_at

report_findings/{auditId}
  findings: [ {code, ordinal, consequence_text} ]   # exactly 3, human-selected

check_defs/{code}
  section, title, full_credit, points, automation, sort_order, enabled

suppressions/{id}
  match_type: place_id|domain|phone|email
  match_value, reason, created_at

outreach/{prospectId}
  touches: [ {no, evidence_code, draft_body, sent_at, replied_at} ]

api_cache/{hash}
  provider, response, fetched_at, expires_at
```

**Required composite indexes.** Create these on day one, because Firestore fails the
query rather than degrading and you will find out at the worst moment:
- `audits`: `batch_id ASC, segment ASC, scores.booked ASC`
- `prospects`: `market_id ASC, gate_result ASC, suppressed ASC`

**Check definitions live in Firestore, not in code.** You will retune weights after
the first batch. That should be a document edit, not a deploy. Checks return
pass/fail only; the scoring function multiplies by `check_defs.points`.

---

## 6. Pipeline

### `ingest_market`
1. Places text search, `roofing contractor in {market}`, paginate to 60.
2. Place Details for phone, website, hours, review summary.
3. Upsert on `place_id`.
4. Check suppressions by place_id, domain, phone. Mark and skip.
5. Publish one `gate_prospect` message per survivor.

### `gate_prospect`
1. Evaluate the fit gate from Places plus a single homepage fetch.
2. Write `gate_result` and `gate_reasons`.
3. On pass or review, publish `run_audit`.

### `run_audit`
Stages in order. A stage failure marks its checks `skipped` and continues. Only a
Firestore failure aborts.

```
1. recon      robots.txt, homepage, sitemap, up to 25 internal pages prioritized by
              /roof-replacement /repair /storm /insurance /financing /about
              /reviews /contact /service-area /*-city
              Respect robots.txt. 2 req/sec per host. 15s timeout.
              UA: RelayAuditBot/1.0 (+https://relayforroofers.com/bot)
2. render     POST renderer with homepage and contact page
3. speed      PageSpeed Insights, mobile
4. ads        Meta Ad Library
5. serp       [after Aug 31] DataForSEO for the two defined queries
6. checks     run every enabled check, write audit_checks
7. score      section scores, band, segment
8. evidence   upload to GCS, write evidence docs
```

### `rank_batch`
Sorts by segment priority, then Booked ascending within segment. Produces the call
list. Drafts openers for the top twenty.

### Concurrency and limits
- Pub/Sub fan-out with max 4 concurrent audits. Politeness matters more than speed.
- Per-host: 2 concurrent requests, 2 req/sec.
- Per-provider rate limiters from config.
- Retries: exponential backoff, 3 attempts, then dead-letter topic.

### Caching
Every provider call goes through a read-through cache keyed on
`sha256(provider + request)`. TTLs: places 30d, psi 3d, serp 7d, ad library 7d.
A `--fresh` flag bypasses for a single prospect.

---

## 7. Scoring

Pure module, no I/O, fully tested. This is what gets retuned, so it must be trivial
to test.

```python
@dataclass
class Score:
    found: int; chosen: int; booked: int
    found_max: int; chosen_max: int; booked_max: int   # excludes skipped
    total: int          # normalized to 100
    band: str
    segment: str | None
    partial: bool

def compute(outcomes: list[CheckOutcome]) -> Score: ...
```

- Section score = points for `pass`, over the section max excluding `skipped` and
  `error`.
- Normalize each section to its nominal weight (30/30/40) before summing.
- `partial` when any section has more than 20% of its points skipped.
- Bands and segments exactly as defined in the criteria doc.
- **If `partial` and the partial section is Booked, segment is `None`.** The UI shows
  "incomplete" rather than a segment. Never segment on incomplete Booked data, since
  that is the entire basis for prioritization.

---

## 8. Reports

Route: `/r/{slug}`, public, unauthenticated, unguessable 16-character slug.

- `noindex,nofollow` in meta and in the `X-Robots-Tag` header.
- Server-side data access only. The public payload carries business name, city, the
  three selected findings with evidence URLs, and the calculator link. **No scores,
  no band, no segment.** Those are internal.
- **Findings are selected by a human**, not by score rank. The highest-point failure
  is not always the most persuasive, and auto-selection turns reports into Lighthouse
  dumps. Operator picks three and writes each consequence line; the model drafts,
  the human approves.
- Evidence via signed GCS URLs, 30-day expiry, regenerated per load.
- Calculator link to `/tools/lead-leakage-calculator/`. Coordinate the query
  parameter name with the existing tool before building this.
- Log a view. Hash the IP with a server-side salt. Never store a raw IP.
- Mobile first, 16px minimum. Brand tokens: asphalt `#16120E`, chalk `#ECE6DC`,
  safety orange `#F25C1F`. Barlow Condensed display, Work Sans body.

**Enforced in code:**
- A test fails the build if any report component string contains an em-dash.
- A runtime assertion caps findings at three.
- Publish is blocked if any selected finding lacks stored evidence.

---

## 9. Outreach

`[after Aug 31]` for anything beyond drafting.

- Suppression checked before every action, including draft generation. A suppressed
  prospect cannot have a draft rendered.
- Touches at day 0, 3, 7, 14, each adding exactly one new finding. Then stop.
- **No sending in v1.** No email API in the outreach path. Drafts are copied by a
  human. Do not automate a message whose reply rate is unknown.

---

## 10. Testing

- `scoring`: table-driven, every band boundary, every segment rule, partial handling.
  100% coverage, no exceptions.
- `checks`: one test per check against a committed HTML fixture. Capture real roofing
  sites once, commit the HTML, never fetch in tests.
- `gate`: table-driven, including storm-chaser and commercial-only cases.
- Phone normalization: at least 20 real-world format variants including extensions
  and call-tracking swaps.
- `cache`: TTL expiry and bypass.
- Report DTO: assert scores and segments never appear in the public payload.
- A `seed` command loading three synthetic prospects, one per segment, so the
  dashboard is never empty in development.

---

## 11. Configuration

```
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=
GOOGLE_CLOUD_LOCATION=us-central1
GEMINI_MODEL=gemini-3.5-flash
GOOGLE_API_KEY=              # only when USE_VERTEXAI=FALSE

RENDERER_URL=
RENDERER_SHARED_SECRET=
GCS_EVIDENCE_BUCKET=

GOOGLE_PLACES_API_KEY=
PAGESPEED_API_KEY=
META_ADS_ACCESS_TOKEN=
DATAFORSEO_LOGIN=            # after Aug 31
DATAFORSEO_PASSWORD=

CRAWL_USER_AGENT=RelayAuditBot/1.0 (+https://relayforroofers.com/bot)
MAX_CONCURRENT_AUDITS=4
REPORT_IP_SALT=
```

Fail fast at boot on any missing required variable. Secrets in Secret Manager. No
`.env` committed. `.env.example` fully keyed and fully blank.

---

## 12. Deployment

- Both services on Cloud Run, min instances 0, max 2, `us-central1`.
- Agent service 512MB, renderer 2GB.
- Dedicated service account with `aiplatform.user`, `datastore.user`,
  `storage.objectAdmin`, `pubsub.editor`, `secretmanager.secretAccessor`. No key
  files anywhere.
- Cloud Scheduler hits `/tick` daily.
- **Protect the public Cloud Run URLs.** An unauthenticated endpoint that triggers
  crawls and model calls is a credit drain waiting to happen.
- `GET /healthz` on both services returning the build SHA.
- Budget alert at $50.

---

## 13. Guardrails

Treat a violation as a failing build.

1. **Never submit a form.** Filling to test validation is fine. Submitting sends a
   real person a real notification. The probe is a separate, manual, paid product.
2. **No automated outreach sending in v1.**
3. **Suppression checked before every outreach action**, drafts included.
4. **No invented numbers.** Unknown means the field is absent. Not zero, not an
   estimate.
5. **No raw IPs stored.**
6. **robots.txt respected.** Disallowed means skipped and noted. No UA spoofing.
7. **Public reports expose no scores, bands, or segments.**
8. **No em-dashes in user-visible strings.** Enforced by test.
9. **No mechanism language in report copy.** Describe what a homeowner would
   experience, never how we detected it or how we would fix it.
10. **Every finding shown to a prospect has stored evidence**, asserted at publish
    time.
