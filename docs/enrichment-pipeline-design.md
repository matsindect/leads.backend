# Enrichment Pipeline Design

**Status:** Draft for review
**Author:** Architecture review
**Scope:** `src/modules/enrichment/` and its collaborators
**Feature flag:** `LEADS_ENABLE_ENRICHMENT` (default `false`)

This document describes the enrichment subsystem of the Lead Pipeline: how it
already fits the modular monolith, and the incremental work needed to move it
from "wired but disabled" to "production-hardened and optional."

It is a design proposal — not a changelog. Each recommendation cites the
existing file it builds on, so reviewers can trust that the pieces slot into
the current architecture instead of replacing it.

---

## 1. Architecture summary (current state)

The repo is a **modular monolith** (one FastAPI process, two logical modules)
that was collapsed from a prior microservice layout. Postgres is the durable
queue; the in-process event bus is a fast-path optimisation.

### Process topology

- **Factory**: [`create_app()`](../src/main.py#L105) with an async
  [`lifespan`](../src/main.py#L50-L102) context that owns the `Container`.
- **DI wiring**: [`Container`](../src/container.py#L29-L138) constructs every
  I/O client exactly once. Enrichment collaborators (`llm_provider`,
  `company_resolver`, `pipeline`) are only built when
  `settings.enable_enrichment` is `true`.
- **Entry-point guard**: [`main.py:70`](../src/main.py#L70-L77) launches
  background workers only when enrichment is on. When off, scraping runs
  unchanged.

### Scraping module (`src/modules/scraping/`)

- Adapters under [`modules/scraping/adapters/`](../src/modules/scraping/adapters/)
  (Reddit, HN, HN-hiring, Wellfound, ProductHunt, RSS-multi, Google CSE,
  LinkedIn/RapidAPI, funding, RemoteOK). Each one implements the
  [`SourceAdapter` Protocol](../src/domain/interfaces.py#L27-L71).
- [`ScrapeOrchestrator.run()`](../src/modules/scraping/orchestrator.py#L37-L128)
  does `fetch → normalize → dedup-insert → emit LeadCreated`.
- Dedup via `(source, source_id)` unique + `dedup_hash` unique
  ([`postgres_repo.py:68-69`](../src/infrastructure/postgres_repo.py#L68-L69)).
- Circuit breaker driven off `scrape_runs` history
  ([`orchestrator.py:130-144`](../src/modules/scraping/orchestrator.py#L130-L144)).

### Enrichment module (`src/modules/enrichment/`)

Already exists as a 6-stage pipeline — the design below hardens and completes
it rather than inventing a new one.

[`EnrichmentPipeline`](../src/modules/enrichment/pipeline.py#L31-L75) composes:

1. [`FetchStage`](../src/modules/enrichment/stages/fetch.py) — load lead,
   guard idempotency, claim via `status='enriching'`.
2. [`ResolveCompanyStage`](../src/modules/enrichment/stages/resolve_company.py)
   — cheap LLM call to pull `company_name / company_domain`, cached in
   `company_resolutions`.
3. [`EnrichCompanyStage`](../src/modules/enrichment/stages/enrich_company.py)
   — HTTP HEAD + title scrape, cached 30 days in `company_enrichments`.
4. [`ClassifyStage`](../src/modules/enrichment/stages/classify.py) — smart LLM
   call for structured signal/ICP/urgency/stack, validated by Pydantic,
   gated by a daily USD budget.
5. [`ScoreStage`](../src/modules/enrichment/stages/score.py) — pure function
   [`compute_final_score()`](../src/modules/enrichment/scoring.py#L15-L56)
   combining signal × ICP × urgency × decision-maker × stack × recency.
6. [`PersistStage`](../src/modules/enrichment/stages/persist.py) — upsert
   `lead_enrichments`, update `raw_leads.score/status='scored'`, publish
   `LeadScored`.

### Transport & persistence

- [`EventBus`](../src/application/bus.py#L21-L87): in-process
  `asyncio.Queue` per event type, bounded (default 1000 — `LEADS_EVENT_BUS_QUEUE_SIZE`),
  non-blocking publish that logs-and-drops on overflow.
- [`EnrichmentWorker`](../src/application/workers.py#L34-L108) consumes
  `LeadCreated`, bounded by `asyncio.Semaphore(max_concurrent_enrichments)`,
  and has its own consecutive-failure **circuit breaker** (10 failures →
  5-min pause).
- [`PendingLeadsResweeper`](../src/application/workers.py#L111-L163) is the
  **durable-queue safety net**: every `LEADS_RESWEEPER_INTERVAL_SECONDS`, it
  selects `raw_leads` rows stuck in `new / pending_enrichment / budget_paused`
  older than 10 min and re-publishes `LeadCreated`.
- Postgres is the source of truth. The `raw_leads.status` column is the real
  queue; `lead_enrichments`, `company_enrichments`, `company_resolutions`,
  `llm_call_log` are the supporting tables (migration
  [`002_add_enrichment_tables.py`](../alembic/versions/002_add_enrichment_tables.py)).

### Cross-cutting concerns already in place

- **Config**: single [`Settings`](../src/config.py) class, Pydantic Settings,
  `LEADS_` env prefix.
- **Logging**: structlog with JSON renderer in prod
  ([`main.py:25-47`](../src/main.py#L25-L47)); every stage uses
  `logger.bind(lead_id=..., stage=...)`.
- **LLM abstraction**: [`LLMProvider`](../src/domain/interfaces.py#L151-L162)
  + [`ModelHint.CHEAP|SMART`](../src/domain/interfaces.py#L144-L148).
  Anthropic & OpenAI implementations; never hardcode a model ID.
- **HTTP**: shared `httpx.AsyncClient` with HTTP/2 and pooling
  ([`container.py:47-52`](../src/container.py#L47-L52)), retrying
  [`HttpFetcher`](../src/infrastructure/fetchers/http.py).
- **Browser**: [`BrowserPool`](../src/infrastructure/fetchers/browser.py#L35-L145)
  is optional and **only constructed when `LEADS_ENABLE_BROWSER_FETCHER=true`**
  ([`container.py:80-88`](../src/container.py#L80-L88)). Lazy Playwright
  import so the base dependency tree stays thin.

### What's missing today

Given the above is already committed, the real "design" work is filling the
gaps that keep this from being production-ready:

1. **No stage-level retries / backoff** — a transient LLM 5xx fails the whole
   pipeline run.
2. **Enrichment skips on idempotency collisions but doesn't differentiate
   terminal vs transient failures** — `enrichment_failed` is defined in the
   DB check constraint but no stage writes it.
3. **Browser fetcher is wired for scraping adapters only** — no enrichment
   stage uses it, even though homepage scraping would benefit.
4. **No per-provider rate limiting** for enrichment-triggered HTTP or LLM
   calls (only `max_concurrent_enrichments` clamp).
5. **No enrichment-provider abstraction** — `EnrichCompanyStage` hardcodes
   the "HEAD + title" strategy; adding Clearbit / Hunter / BuiltWith later
   means editing the stage.
6. **Body retention / PII hygiene** — the full post body is copied into
   `raw_leads.body` and into the LLM prompt. No truncation, redaction, or
   TTL.
7. **Observability**: `EnrichmentWorker.stats` exists but is not exposed
   on any `/health` route.

The rest of this document is the plan for (1)–(7).

---

## 2. Enrichment pipeline design

### 2.1 What to enrich per lead

Keep the current shape; add a few first-class fields to support downstream
outreach.

| Field                         | Source                                           | Stage                | Notes                                  |
|-------------------------------|--------------------------------------------------|----------------------|----------------------------------------|
| `company_name`, `company_domain` | Adapter → else LLM cheap call                 | Resolve              | Cached in `company_resolutions`        |
| `company_reachable`, `homepage_title` | HTTP HEAD/GET                           | EnrichCompany        | 30-day TTL in `company_enrichments`    |
| `company_stage`, `employee_count`, `funding_stage` | Provider plug-ins           | EnrichCompany (new)  | Optional, provider-gated               |
| `refined_signal_type`, `refined_signal_strength` | LLM smart                       | Classify             | Pydantic-validated                     |
| `icp_fit_score`, `decision_maker_likelihood`, `urgency_score` | LLM smart          | Classify             | 0–100 ints                             |
| `extracted_stack`             | LLM smart                                        | Classify             | Used by stack-match scoring            |
| `pain_summary`, `recommended_approach`, `skip_reason` | LLM smart                | Classify             |                                        |
| `final_score`                 | Pure function                                    | Score                | See [`scoring.py`](../src/modules/enrichment/scoring.py) |

**Not stored**: prompt text, raw LLM responses, full HTML of homepages.
Only derived/scalar fields. See §2.11.

### 2.2 Sync vs async

Enrichment is **entirely async** relative to ingestion. Ingestion ends when
the scraping orchestrator inserts rows and publishes `LeadCreated` —
ingestion must never wait on enrichment, and must not break when
enrichment is disabled.

Within a single pipeline run, stages are sequential but each stage is an
async coroutine. Concurrency comes from multiple pipeline runs across leads
(bounded by `max_concurrent_enrichments`), not intra-stage parallelism.

This is already the current contract — keep it.

### 2.3 Queueing, retries, dedup, rate limits

**Queue**: the `raw_leads.status` column is the durable queue. The
[`EventBus`](../src/application/bus.py) is an opportunistic fast path; the
[`PendingLeadsResweeper`](../src/application/workers.py#L111-L163) is the
recovery mechanism. Do not re-introduce Redis Streams (see memory note).

**Dedup**:
- Ingestion-level: `dedup_hash` unique constraint in `raw_leads`.
- Pipeline-level: [`FetchStage`](../src/modules/enrichment/stages/fetch.py)
  short-circuits with `AlreadyProcessedError` when `status ∈ {scored, sent,
  closed, dead}`. **Add**: also short-circuit when `status == 'enriching'`
  and `enriched_at IS NULL AND updated younger than 5 min` — prevents two
  workers from racing on the same lead after a bus replay.
- Resolver-level: `company_resolutions.cache_key` (title+body[:500] hash).
- Company-level: `company_enrichments.domain` with `expires_at`.

**Retries (proposed)**:
- **Stage-local**: wrap each stage's external call in
  `tenacity.AsyncRetrying` with exponential backoff + jitter. Retry on
  `httpx.TransportError`, provider-specific 429/5xx; do not retry on 4xx
  other than 429.
- **Pipeline-level**: on any unhandled exception, transition the lead to
  `enrichment_failed` and record an attempt count. The resweeper re-enqueues
  `enrichment_failed` **up to N times** (new: `enrichment_max_attempts`,
  default 3) before moving to `dead`.

**Rate limits (proposed)**:
- Per-provider async token bucket
  ([`infrastructure/rate_limit.py`](../src/infrastructure/), new). The LLM
  provider wraps `complete_structured` in it so both Anthropic and OpenAI
  respect configured RPS without the stages knowing.
- Reuse the [`RateLimitedError`](../src/infrastructure/fetchers/base.py)
  already defined in the fetchers module.
- Per-domain limits for `EnrichCompanyStage`: one outbound request per
  domain per minute (in-memory `dict[str, asyncio.Semaphore]`).

### 2.4 Failure recording without breaking ingestion

- Ingestion NEVER awaits enrichment. [`ScrapeOrchestrator`](../src/modules/scraping/orchestrator.py)
  publishes `LeadCreated` **after** commit; if the bus is full, the publish
  is dropped and the resweeper picks it up later
  ([`bus.py:43-49`](../src/application/bus.py#L43-L49)).
- Enrichment failures write to **two places**:
  1. `raw_leads.status = 'enrichment_failed'` + bump `enrichment_attempts`
     (new column, §4).
  2. `enrichment_attempts` row logging the exception class, stage name,
     truncated message, attempt number, and `failed_at` (new table, §4).
- The worker never propagates enrichment exceptions up to the bus loop — it
  already catches broadly ([`workers.py:85-97`](../src/application/workers.py#L85-L97))
  and the consecutive-failure counter drives the pause. Keep that.

### 2.5 Interaction with scoring

Scoring stays a **pure function**
([`scoring.py`](../src/modules/enrichment/scoring.py)) — zero I/O, easy to
unit-test, easy to re-run against historical enrichments. This is a feature,
not an accident. Additions should extend the pure function; any new input
must be deterministic and already materialised in the
[`PipelineContext`](../src/domain/models.py#L97-L111) by the time the
`ScoreStage` runs.

New optional inputs (all keep current weights if absent):
- `company_stage` weight — seed/series-A leads score higher than public co's.
- `funding_recency` — days since last funded, plug into a sigmoid.

Weights remain colocated in `scoring.py` with explanatory comments — do not
push them to env. They are tuning knobs the owner iterates on weekly, not
deployment parameters.

### 2.6 Database schema changes

Net-new columns & tables. One new Alembic revision `004_harden_enrichment.py`:

```python
# raw_leads — track retry state explicitly
op.add_column("raw_leads", sa.Column("enrichment_attempts", sa.SmallInteger, server_default="0", nullable=False))
op.add_column("raw_leads", sa.Column("last_enrichment_error_at", sa.DateTime(timezone=True)))

# New: per-attempt error log, bounded retention
op.create_table(
    "enrichment_attempts",
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("lead_id", UUID(as_uuid=True), sa.ForeignKey("raw_leads.id", ondelete="CASCADE"), nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("error_class", sa.Text, nullable=False),
    sa.Column("error_message", sa.Text),   # truncated to 1 KB
    sa.Column("attempt", sa.SmallInteger, nullable=False),
    sa.Column("failed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
)
op.create_index("idx_enrichment_attempts_lead", "enrichment_attempts", ["lead_id", sa.text("failed_at DESC")])

# Extend company_enrichments with optional provider-backed fields
op.add_column("company_enrichments", sa.Column("industry", sa.Text))
op.add_column("company_enrichments", sa.Column("country", sa.Text))
op.add_column("company_enrichments", sa.Column("provider", sa.Text))  # 'http_probe' | 'clearbit' | ...

# Replace the old raw_leads_status_check with one that includes 'dead' already present
# and a new 'max_attempts_reached' terminal — kept separate from 'dead' so we can
# distinguish "we gave up" from "manually killed".
op.drop_constraint("raw_leads_status_check", "raw_leads", type_="check")
op.create_check_constraint(
    "raw_leads_status_check",
    "raw_leads",
    "status IN ('new','pending_enrichment','enriching','scored','enrichment_failed',"
    "'budget_paused','queued','sent','closed','dead','max_attempts_reached')",
)
```

**Retention**: `enrichment_attempts` rows older than 30 days are pruned by a
daily housekeeping task (§2.10). `llm_call_log` rows older than 90 days the
same way.

### 2.7 Config / env variables

Add to [`config.py`](../src/config.py) with sensible defaults:

```python
# --- Enrichment retries & rate limits ---
enrichment_max_attempts: int = 3
enrichment_stage_retry_max: int = 3          # per-stage tenacity retries
enrichment_stage_retry_wait_sec: float = 2.0  # base for expo backoff
enrichment_http_per_domain_rps: float = 1.0

# --- LLM rate limits (per provider) ---
llm_requests_per_second: float = 2.0
llm_burst: int = 5

# --- Retention ---
enrichment_attempts_retention_days: int = 30
llm_call_log_retention_days: int = 90

# --- Body hygiene ---
enrichment_body_char_limit: int = 4000   # trim before LLM prompts
enrichment_body_store_limit: int = 8000  # trim at ingestion

# --- Provider gates (all default false/empty) ---
enrichment_provider_clearbit_key: str = ""
enrichment_provider_hunter_key: str = ""
```

Mirror these in [`.env.example`](../.env.example) under a new
`# --- Enrichment (pipeline tuning) ---` block.

### 2.8 Module / file layout

Additive changes only — no renames.

```
src/modules/enrichment/
├── pipeline.py                   # unchanged
├── company_resolver.py           # unchanged
├── scoring.py                    # add company_stage weight
├── providers/                    # NEW — pluggable company data providers
│   ├── __init__.py
│   ├── base.py                   # CompanyDataProvider Protocol
│   ├── http_probe.py             # the current HEAD+title logic, extracted
│   ├── clearbit.py               # optional, disabled w/o LEADS_*_KEY
│   └── hunter.py                 # optional, disabled w/o LEADS_*_KEY
├── browser_enricher.py           # NEW — wraps BrowserFetcher for homepage scrapes
├── retry.py                      # NEW — tenacity policies, shared
└── stages/
    ├── fetch.py                  # add 'enriching too long' guard
    ├── resolve_company.py        # unchanged
    ├── enrich_company.py         # delegate to providers chain
    ├── classify.py               # wrap LLM call in retry.py
    ├── score.py                  # unchanged
    └── persist.py                # also records enrichment_attempts reset

src/infrastructure/
├── rate_limit.py                 # NEW — async token bucket
└── postgres_repo.py              # add record_attempt, prune_old_attempts

src/application/
└── workers.py                    # add HousekeepingWorker
```

New Protocols in [`domain/interfaces.py`](../src/domain/interfaces.py):

```python
class CompanyDataProvider(Protocol):
    """Pluggable provider of structured company data."""
    @property
    def name(self) -> str: ...
    @property
    def is_enabled(self) -> bool: ...
    async def enrich(self, domain: str) -> dict[str, Any] | None: ...
```

`EnrichCompanyStage` now composes a **chain**: `[HttpProbeProvider,
ClearbitProvider, HunterProvider, ...]`. Each provider is consulted in order;
first non-empty merge wins for each field. Cache key stays `domain`, with
`provider` recorded for audit.

### 2.9 Provider abstraction

- Every provider implements `CompanyDataProvider` — not inherits from a base
  class.
- Disabled providers return `is_enabled == False`; the container filters
  them at build time (`container.py`). A zero-length chain is valid and
  degrades to "no enrichment data" without error — graceful degradation.
- Providers never share an HTTP client with browser scraping. They use the
  container's `http_client` plus their own rate limiter keyed on `name`.
- **Adding a provider**: new file under `providers/`, implement the Protocol,
  register it in `container.py` behind its own `LEADS_ENRICHMENT_PROVIDER_*_KEY`.
  No edits to `EnrichCompanyStage`.

### 2.10 Browser fetching isolation

- Playwright stays an **optional** pip extra (already is —
  [`browser.py:52-59`](../src/infrastructure/fetchers/browser.py#L52-L59)).
- `BrowserPool` is only instantiated when `LEADS_ENABLE_BROWSER_FETCHER=true`
  ([`container.py:80-88`](../src/container.py#L80-L88)). No change.
- **New**: `modules/enrichment/browser_enricher.py` — injected with
  `browser_fetcher: BrowserFetcher | None`. When `None`, it's a no-op
  provider that returns `{}`. Stages use it through the provider Protocol,
  so the stage code has zero conditionals on `enable_browser_fetcher`.
- Browser-based enrichment is **opt-in per domain**: maintain a small
  allow-list (`LEADS_ENRICHMENT_BROWSER_DOMAINS=[]`, default empty) to avoid
  spinning up Chromium for every lead.

### 2.11 Sensitive / excessive data handling

Rules:

1. **Trim at the gate**: `ScrapeOrchestrator` passes at most
   `enrichment_body_store_limit` chars of `body` into `raw_leads.body`.
   Implement in the `CanonicalLead` normalize step, not in SQL — cheaper to
   reason about.
2. **Trim again at the prompt**: the classifier uses at most
   `enrichment_body_char_limit` chars, regardless of what is stored.
   Prompts live in [`src/prompts/`](../src/prompts/) — add `| truncate`
   filter usage.
3. **Don't persist prompts or raw responses.** Only the validated,
   Pydantic-modelled fields from `ClassificationResponse` are stored.
4. **PII redaction (best-effort)**: before sending to the LLM, run a
   conservative regex pass over emails and phone numbers in `body`,
   replacing them with tokens (`<email>`, `<phone>`). Applied centrally in
   `modules/enrichment/sanitize.py` (new, pure function, unit-tested).
5. **LLM log retention**: `llm_call_log` keeps token counts and cost only.
   No prompt / completion text — already true today, keep it.
6. **HTML from browser fetcher**: never stored. Parsed in-memory, only
   derived scalars persisted.

### 2.12 Observability

- Every stage already uses structlog `bind(lead_id=..., stage=...)` — extend
  this to include `attempt` number.
- Add `/api/v1/health/enrichment` exposing `EnrichmentWorker.stats`,
  resweeper last-run, `enrichment_failed` count in last hour, and
  `get_daily_llm_cost()`.
- Add `enrichment_duration_ms` to `PersistStage` (already have `started_at`
  via pipeline context — add it to the dataclass).

---

## 3. Step-by-step implementation plan

Each step is a small PR. Items in **bold** are the minimum needed to enable
enrichment in production.

1. **Migration `004_harden_enrichment.py`**
   (`enrichment_attempts` table, `raw_leads.enrichment_attempts` column,
   updated status check).
2. **Retry policies**: add `modules/enrichment/retry.py` (tenacity wrappers
   for LLM and HTTP). Wire into `ClassifyStage`, `ResolveCompanyStage`,
   `EnrichCompanyStage`.
3. **Failure path**: add `EnrichmentRepository.record_attempt()` +
   `increment_attempts()`. Modify
   [`workers.py:85-97`](../src/application/workers.py#L85-L97) to write
   `enrichment_failed` + audit row. Resweeper promotes to
   `max_attempts_reached` when `enrichment_attempts >= enrichment_max_attempts`.
4. **Provider chain**: extract current HEAD+title logic into
   `providers/http_probe.py`. Introduce `CompanyDataProvider` Protocol.
   Refactor `EnrichCompanyStage` to iterate the chain.
5. **Rate limiting**: `infrastructure/rate_limit.py` (async token bucket).
   Wrap LLM providers; wrap per-domain HTTP in `http_probe.py`.
6. **Body hygiene**: enforce `enrichment_body_store_limit` in normalize
   step; add `modules/enrichment/sanitize.py` for PII redaction; call from
   `ClassifyStage` before rendering the prompt.
7. **Optional browser enricher**: `browser_enricher.py` + allow-list env.
   No-op when `LEADS_ENABLE_BROWSER_FETCHER=false`.
8. **Optional providers**: `clearbit.py`, `hunter.py`, each behind their own
   API-key env var and `is_enabled` check.
9. **Housekeeping**: `HousekeepingWorker` in `application/workers.py` —
   daily prune on `enrichment_attempts` and `llm_call_log`.
10. **Observability endpoint**: `GET /health/enrichment` in
    [`api/routes.py`](../src/api/routes.py) + schema in
    [`api/schemas.py`](../src/api/schemas.py).
11. **Documentation**: add a `docs/CHANGELOG.md` entry (per the repo rule);
    update `.env.example`.

Steps 1–3 unblock flipping `LEADS_ENABLE_ENRICHMENT=true` in staging.
Steps 4–7 are hardening. Steps 8–11 are polish.

---

## 4. Test plan

Follow existing style — `tests/test_*.py`, pytest, in-memory stubs of the
Protocols. Current suite already has good coverage (`test_enrichment_stages.py`,
`test_enrichment_integration.py`) — extend rather than replace.

**Unit**
- `test_scoring.py` (exists): add cases for `company_stage` weight,
  skip-penalty cap at 15.
- `test_sanitize.py` (new): email/phone regex redaction preserves meaning.
- `test_rate_limit.py` (new): token-bucket burst and refill.
- `test_retry.py` (new): tenacity policy retries on `TransportError`, not on
  `401`.
- `test_providers.py` (new): provider chain short-circuits on first non-empty,
  skips disabled providers, graceful on all-fail.

**Integration** (against test Postgres, as existing `test_enrichment_integration.py`)
- End-to-end pipeline with LLM mock: new lead → `scored` status, `llm_call_log`
  row, `lead_enrichments` row.
- Failure path: LLM raises → `enrichment_failed`, attempts row written, third
  failure promotes to `max_attempts_reached`.
- Budget exceeded mid-run → `budget_paused`, no enrichment row.
- Resweeper: lead stuck in `enriching` older than cutoff → re-published.
- Browser-disabled: `BrowserPool` not constructed; enrichment still succeeds.

**Contract**
- With `LEADS_ENABLE_ENRICHMENT=false`: no worker tasks in `app.state.worker_tasks`,
  no LLM provider in container, ingestion writes `status='new'` and stays
  there. Regression guard for "enrichment off" parity.

**Load** (manual, pre-production)
- 500 synthetic leads seeded with a mock LLM returning fixed payloads.
  Verify: pipeline concurrency respects `max_concurrent_enrichments`, queue
  drops do not lose leads (resweeper recovers), circuit breaker trips and
  recovers.

---

## 5. Risks, open questions, tradeoffs

### Risks
- **LLM cost blow-ups.** The daily budget gate is lead-scoped, not
  provider-scoped — a bad prompt template could still generate high tokens
  per call. Mitigation: per-stage token caps (new: `classify_max_output_tokens`).
- **Browser fetcher memory leaks** are real even with recycle-after-100.
  Keep the allow-list narrow until we have memory telemetry.
- **Stale cache**: `company_enrichments` expires in 30 days, but a company
  that pivots or shuts down inside that window will produce wrong scores.
  Acceptable at current scale; revisit if leads > 10k/week.

### Open questions
- Should `ResolveCompanyStage` and `ClassifyStage` be merged into a single
  LLM call once we trust the smart model's structured output? (Would cut
  per-lead cost ~30%, at the price of losing the cheap-model cache hit.)
- Do we want per-adapter enrichment toggles (e.g. skip enrichment for
  `funding` leads that already have company info)? Trivial to add; needs
  product input.
- `user_skills` is a single list — eventually this should be per-workspace,
  which would require a `workspaces` table. Not needed at single-developer
  scale.

### Tradeoffs accepted
- **No intra-stage parallelism.** Running resolve + http-probe concurrently
  would cut 200–400ms per lead but complicates error handling and cache
  semantics. Not worth it at current throughput.
- **In-process bus, not Kafka/Redis Streams.** Explicit decision (see
  project memory). The resweeper makes this safe; scaling out requires
  moving to a true broker, and that's a future-problem.
- **Pure-Python scoring, not a model.** Tunable by hand, auditable,
  deterministic. We lose the ability to learn weights from conversion
  data — fine until we have conversion data worth learning from.

---

## Appendix A — Files referenced

| Concern                       | File                                                              |
|-------------------------------|-------------------------------------------------------------------|
| App factory & lifespan        | [`src/main.py`](../src/main.py)                                   |
| DI wiring                     | [`src/container.py`](../src/container.py)                         |
| Settings                      | [`src/config.py`](../src/config.py)                               |
| Domain models                 | [`src/domain/models.py`](../src/domain/models.py)                 |
| Protocols                     | [`src/domain/interfaces.py`](../src/domain/interfaces.py)         |
| Events                        | [`src/domain/events.py`](../src/domain/events.py)                 |
| Event bus                     | [`src/application/bus.py`](../src/application/bus.py)             |
| Workers                       | [`src/application/workers.py`](../src/application/workers.py)     |
| Pipeline                      | [`src/modules/enrichment/pipeline.py`](../src/modules/enrichment/pipeline.py) |
| Stages                        | [`src/modules/enrichment/stages/`](../src/modules/enrichment/stages/) |
| Scoring                       | [`src/modules/enrichment/scoring.py`](../src/modules/enrichment/scoring.py) |
| Company resolver              | [`src/modules/enrichment/company_resolver.py`](../src/modules/enrichment/company_resolver.py) |
| Repository                    | [`src/infrastructure/postgres_repo.py`](../src/infrastructure/postgres_repo.py) |
| LLM providers                 | [`src/infrastructure/anthropic_provider.py`](../src/infrastructure/anthropic_provider.py), [`src/infrastructure/openai_provider.py`](../src/infrastructure/openai_provider.py) |
| Fetchers                      | [`src/infrastructure/fetchers/`](../src/infrastructure/fetchers/) |
| Prompts                       | [`src/prompts/`](../src/prompts/)                                 |
| Migrations                    | [`alembic/versions/`](../alembic/versions/)                       |
