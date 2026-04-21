# Enrichment Pipeline Design

**Status:** Draft for review (v2 — rewritten around 9-stage pipeline)
**Author:** Architecture review
**Scope:** `src/modules/enrichment/` and its collaborators
**Feature flag:** `LEADS_ENABLE_ENRICHMENT` (default `false`)

This document describes the enrichment subsystem of the Lead Pipeline: how it
already fits the modular monolith, and the incremental work needed to move it
from "wired but disabled" to "production-hardened and optional."

**Why v2.** The v1 draft assumed enrichment could compensate for thin scraper
output with an LLM + HTTP HEAD probe. That's not enough: half of the adapters
(Google CSE, RSS, LinkedIn posts, Reddit, HN) hand us pointers — titles,
URLs, short snippets, usernames — not content. Asking a SMART LLM to score
`icp_fit / decision_maker_likelihood / urgency / extracted_stack` from a
150-char Google snippet produces hallucination, not signal.

v2 adds three stages (**DeepFetch**, **EnrichPerson**, **Sufficiency**) and
promotes browser fetching from "optional extra" to "recommended production
default," with provider abstractions growing from one Protocol to three.

It is a design proposal — not a changelog. Each recommendation cites the
existing file it builds on so reviewers can trust that the pieces slot into
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
  `settings.enable_enrichment` is `true`. `LinkedInJobEnricher` is already
  wired independently behind `settings.linkedin_rapidapi_key`.
- **Entry-point guard**: [`main.py`](../src/main.py) launches background
  workers only when enrichment is on; when off, scraping runs unchanged.

### Scraping module (`src/modules/scraping/`)

- Ten adapters under [`modules/scraping/adapters/`](../src/modules/scraping/adapters/)
  (Reddit, HN, HN-hiring, Wellfound, ProductHunt, RSS-multi, Google CSE,
  LinkedIn/RapidAPI, funding, RemoteOK). Each one implements the
  [`SourceAdapter` Protocol](../src/domain/interfaces.py#L27-L71).
- [`ScrapeOrchestrator.run()`](../src/modules/scraping/orchestrator.py#L37-L128):
  `fetch → normalize → dedup-insert → emit LeadCreated`.
- Dedup via `(source, source_id)` unique + `dedup_hash` unique.
- Circuit breaker driven off `scrape_runs` history.

### Prospect discovery (existing, separate from enrichment)

The LinkedIn adapter also drives two **prospect** endpoints whose outputs
live in their own tables, not in `raw_leads`:

- `POST /search-companies` → [`target_companies`](../alembic/versions/004_add_target_prospects.py)
- `POST /search-employees` → `target_people`

These are prospect lists, not buying signals, and they don't flow through the
enrichment pipeline. They may later *seed* enrichment for specific campaigns
— out of scope here.

### Current enrichment module (`src/modules/enrichment/`)

Already exists as a **6-stage** pipeline; v2 grows it to **9 stages**.

[`EnrichmentPipeline`](../src/modules/enrichment/pipeline.py#L31-L75) today:

1. [`FetchStage`](../src/modules/enrichment/stages/fetch.py) — load lead,
   guard idempotency, claim via `status='enriching'`.
2. [`ResolveCompanyStage`](../src/modules/enrichment/stages/resolve_company.py)
   — cheap LLM call; cached in `company_resolutions`.
3. [`EnrichCompanyStage`](../src/modules/enrichment/stages/enrich_company.py)
   — HTTP HEAD + title scrape; cached 30 days.
4. [`ClassifyStage`](../src/modules/enrichment/stages/classify.py) — smart
   LLM call, Pydantic-validated, budget-gated.
5. [`ScoreStage`](../src/modules/enrichment/stages/score.py) — pure
   [`compute_final_score()`](../src/modules/enrichment/scoring.py#L15-L56).
6. [`PersistStage`](../src/modules/enrichment/stages/persist.py) — upsert
   `lead_enrichments`, update `raw_leads.score/status='scored'`,
   publish `LeadScored`.

[`LinkedInJobEnricher`](../src/modules/enrichment/linkedin_enricher.py)
exists as a utility (just built) but is not yet called from any stage.

### Transport & persistence

- [`EventBus`](../src/application/bus.py#L21-L87): in-process
  `asyncio.Queue` per event type, bounded (default 1000), non-blocking
  publish that logs-and-drops on overflow.
- [`EnrichmentWorker`](../src/application/workers.py#L34-L108) consumes
  `LeadCreated`, bounded by `asyncio.Semaphore(max_concurrent_enrichments)`,
  and has its own circuit breaker (10 failures → 5-min pause).
- [`PendingLeadsResweeper`](../src/application/workers.py#L111-L163) is
  the durable-queue safety net.
- Postgres is the source of truth. `raw_leads.status` is the real queue;
  `lead_enrichments`, `company_enrichments`, `company_resolutions`,
  `llm_call_log` are the supporting tables.

### What's missing today

1. **Thin inputs.** For Google CSE, RSS, HN links, Reddit titles, LinkedIn
   posts — the scraper stores only pointers. No stage fetches the actual
   content before classification.
2. **No person enrichment.** Lead-scoring depends on
   `decision_maker_likelihood`, which the LLM currently guesses.
3. **No sufficiency gate.** Every lead hits the SMART model, including bot
   posts, 30-char titles, and throwaway accounts — wasting budget.
4. **No stage-level retries / backoff.** A transient LLM 5xx fails the run.
5. **No differentiated failure status.** `enrichment_failed` exists in the
   DB check constraint but no stage writes it.
6. **Browser fetcher is scraping-only.** Wired for Wellfound, not used by
   enrichment.
7. **No per-provider rate limiting** beyond `max_concurrent_enrichments`.
8. **No enrichment-provider abstraction.**
9. **Body retention / PII hygiene** — full post body copied into prompts.
10. **Observability**: `EnrichmentWorker.stats` exists but isn't exposed.

v2 addresses all of these.

---

## 2. v2 pipeline — nine stages

```
 ┌────────┐   ┌───────────┐   ┌─────────┐   ┌───────────────┐   ┌──────────────┐
 │ Fetch  ├──▶│ DeepFetch ├──▶│ Resolve ├──▶│ EnrichCompany ├──▶│ EnrichPerson │
 └────────┘   └───────────┘   └─────────┘   └───────────────┘   └──────┬───────┘
                                                                       │
              ┌────────┐   ┌───────┐   ┌──────────┐   ┌─────────────┐  │
              │Persist │◀──│ Score │◀──│ Classify │◀──│ Sufficiency │◀─┘
              └────────┘   └───────┘   └──────────┘   └─────────────┘
```

### 2.1 Stage contracts

| # | Stage | Purpose | Can skip later stages? |
|---|---|---|---|
| 1 | **Fetch** | Load lead, claim via `status='enriching'`. | No — raises `AlreadyProcessedError` for terminal statuses. |
| 2 | **DeepFetch** ⭐ | Pull richer content per-source. | No — degrades to empty content if fetching fails. |
| 3 | **Resolve** | Extract company name/domain (cheap LLM, cached). | No. |
| 4 | **EnrichCompany** | Run `CompanyDataProvider` chain against `company_domain`. | No — providers return `{}` when unavailable. |
| 5 | **EnrichPerson** ⭐ | Run `PersonDataProvider` chain for poster/hiring-manager. | No — same graceful degradation. |
| 6 | **Sufficiency** ⭐ | Rule-based gate: do we have enough signal to classify? | **Yes** — sets `status='insufficient'` and short-circuits to Persist (skips Classify+Score). |
| 7 | **Classify** | Smart LLM call with all accumulated context. | Raises `BudgetExceededError` → `status='budget_paused'`. |
| 8 | **Score** | Pure `compute_final_score()`. | No. |
| 9 | **Persist** | Upsert `lead_enrichments`, update `raw_leads`, emit `LeadScored`. | No. |

⭐ = new in v2.

### 2.2 Why the order matters

- **DeepFetch before Resolve**: the real page/profile contains the company
  mention far more reliably than a search-result title. Resolve-via-LLM
  accuracy ~doubles when fed a fetched page vs. a snippet.
- **EnrichPerson after EnrichCompany**: some person providers (Hunter,
  Apollo) key on `company_domain`. Running EnrichCompany first means the
  person enricher already has the domain.
- **Sufficiency after all fetches**: the gate needs the full context to
  judge. A lead with a 50-char body is still viable if DeepFetch returned
  2 KB of real content, or if EnrichPerson turned up a CTO-level match.
- **Classify last among LLM calls**: the SMART model sees the most context
  we're going to gather. No point calling it with only Resolve's output.

### 2.3 What gets enriched per lead (v2)

Existing fields unchanged; new ones marked ⭐.

| Field | Source | Stage |
|---|---|---|
| `company_name`, `company_domain` | Adapter → else LLM | Resolve |
| `company_reachable`, `homepage_title` | HTTP probe | EnrichCompany |
| `company_stage`, `employee_count`, `funding_stage`, `industry`, `country` | Provider plug-ins | EnrichCompany |
| ⭐ `person_title`, `seniority_score` | `PersonDataProvider` | EnrichPerson |
| ⭐ `person_email_pattern`, `email_confidence` | Hunter / Apollo | EnrichPerson |
| ⭐ `deep_content_chars` (telemetry only) | DeepFetch | — |
| ⭐ `insufficient_reason` | Sufficiency | (when tripped) |
| `refined_signal_type`, `refined_signal_strength` | LLM smart | Classify |
| `icp_fit_score`, `decision_maker_likelihood`, `urgency_score` | LLM smart | Classify |
| `extracted_stack` | LLM smart | Classify |
| `pain_summary`, `recommended_approach`, `skip_reason` | LLM smart | Classify |
| `final_score` | Pure function | Score |

**Not stored**: full deep-fetched HTML, raw LLM responses, prompt text.
Only scalars and validated structures.

### 2.4 Sync vs async

Enrichment is **entirely async** relative to ingestion. Ingestion ends when
the scraping orchestrator inserts rows and publishes `LeadCreated` —
ingestion must never wait on enrichment, and must not break when enrichment
is disabled.

Within a single pipeline run, stages are sequential; concurrency comes from
multiple pipeline runs across leads (bounded by
`max_concurrent_enrichments`). This matches the current contract — keep it.

### 2.5 Queueing, retries, dedup, rate limits

**Queue**: `raw_leads.status` is the durable queue. The
[`EventBus`](../src/application/bus.py) is an opportunistic fast path; the
[`PendingLeadsResweeper`](../src/application/workers.py#L111-L163) is the
recovery mechanism. Do not re-introduce Redis Streams.

**Dedup**:
- Ingestion: `dedup_hash` unique in `raw_leads`.
- Pipeline: [`FetchStage`](../src/modules/enrichment/stages/fetch.py)
  short-circuits when `status ∈ {scored, sent, closed, dead, insufficient}`.
- Resolver: `company_resolutions.cache_key`.
- Company: `company_enrichments.domain` + TTL.
- ⭐ Person: `person_enrichments.(source, person_id)` + TTL (new table).
- ⭐ DeepFetch: optional per-strategy LRU on `lead_id` — not persistent,
  avoids re-fetching within the same pipeline run on retry.

**Retries**:
- **Stage-local**: each external call wrapped in
  `tenacity.AsyncRetrying` (`modules/enrichment/retry.py`, new).
  Retry on `httpx.TransportError`, 429, 5xx. Don't retry on 401/403/404.
- **Pipeline-level**: on unhandled exception, transition to
  `enrichment_failed`, increment `enrichment_attempts`. Resweeper retries
  up to `enrichment_max_attempts` (default 3), then → `max_attempts_reached`.

**Rate limits**:
- Per-provider async token bucket (`infrastructure/rate_limit.py`, new)
  wrapping LLM providers and each `CompanyDataProvider` / `PersonDataProvider`
  / `DeepFetchStrategy`.
- Per-domain limits for HTTP probes (one outbound per domain per minute).
- Reuse the existing
  [`RateLimitedError`](../src/infrastructure/fetchers/base.py).

### 2.6 Failure recording without breaking ingestion

- Ingestion never awaits enrichment. Publish is non-blocking; overflow is
  logged and recovered by the resweeper.
- Enrichment failures write to two places:
  1. `raw_leads.status = 'enrichment_failed'` + bump `enrichment_attempts`.
  2. `enrichment_attempts` audit row: exception class, stage name, truncated
     message, attempt number, `failed_at`.
- The worker never propagates exceptions to the bus loop; it already catches
  broadly ([`workers.py:85-97`](../src/application/workers.py#L85-L97)).

### 2.7 Interaction with scoring

Scoring stays **pure** ([`scoring.py`](../src/modules/enrichment/scoring.py)).
v2 adds two inputs (both optional, both default-neutral):

- `seniority_score` (0–100) from EnrichPerson — strongly correlates with
  decision-maker likelihood. Weight = 0.10, reducing the LLM's own
  `decision_maker_likelihood` weight from 0.15 → 0.10. The two combine.
- `company_stage` weight — seed / Series-A leads score higher than public.
  Weight = 0.05, drawn from recency.

Final weights (sum = 1.0):

```
signal        0.20
icp           0.20   (was 0.25)
urgency       0.15
dm (LLM)      0.10   (was 0.15)
stack         0.15
recency       0.05   (was 0.10)
seniority ⭐   0.10
company_stage⭐ 0.05
```

Weights stay in `scoring.py` with explanatory comments — not in env. They're
tuning knobs, not deployment parameters.

---

## 3. Provider abstractions

Three Protocols, each pluggable and independently disable-able. All live in
[`src/domain/interfaces.py`](../src/domain/interfaces.py).

### 3.1 `DeepFetchStrategy`

```python
@dataclass(frozen=True, slots=True)
class DeepContent:
    text: str                     # extracted main content, trimmed to N chars
    metadata: dict[str, Any]      # source-specific (e.g. OP history summary)
    char_count: int               # for telemetry (persisted)
    source_strategy: str          # strategy name — for audit/debugging
    fetched_at: datetime

class DeepFetchStrategy(Protocol):
    @property
    def source(self) -> str: ...          # 'reddit' | 'hackernews' | ...
    @property
    def is_enabled(self) -> bool: ...
    @property
    def requires_browser(self) -> bool: ...
    async def fetch(self, lead_data: dict[str, Any]) -> DeepContent: ...
```

Registry is a `dict[str, DeepFetchStrategy]` keyed on `source`. `DeepFetchStage`
picks by `lead.source`; on miss, uses a no-op strategy that returns the
lead's existing `body` as `text`.

### 3.2 `CompanyDataProvider`

```python
class CompanyDataProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def is_enabled(self) -> bool: ...
    async def enrich(self, domain: str) -> dict[str, Any] | None: ...
```

Concrete providers: `HttpProbeProvider` (extracted from current
`EnrichCompanyStage`), `ClearbitProvider`, `BuiltWithProvider`. Chain
semantics: consult in order, merge first non-empty per field, cache union
on `company_enrichments` keyed by domain with `provider` column recording
which provider filled each row.

### 3.3 `PersonDataProvider` ⭐

```python
class PersonDataProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def is_enabled(self) -> bool: ...
    async def enrich(
        self,
        *,
        person_name: str | None,
        person_role: str | None,
        linkedin_url: str | None,
        company_domain: str | None,
    ) -> dict[str, Any] | None: ...
```

Concrete providers:
- `LinkedInPersonProvider` — uses the RapidAPI host (same key as the
  scraping adapter); fetches full profile from `linkedin_url`.
- `HunterProvider` — email + role by name + domain.
- `ApolloProvider` — seniority + title (if a key is configured).

Chain semantics mirror `CompanyDataProvider`.

**Graceful degradation**: if no person providers are enabled, EnrichPerson
returns `{}` and Sufficiency/Classify continue. Keeps the pipeline usable
with zero person-data API keys.

---

## 4. Browser fetching — first-class, not an extra

The v1 doc left `LEADS_ENABLE_BROWSER_FETCHER=false` as the recommended
default. v2 flips that: **enable browser fetching in production** for full
enrichment coverage, while keeping the dependency optional in the package
sense (Playwright stays a `[browser]` extra).

### Why

- **LinkedIn profile deep-fetch** is the highest-value person enrichment,
  and the RapidAPI host doesn't expose every profile field — we need the
  actual profile page for some.
- **Wellfound, ProductHunt, Angel.co** all require JS execution.
- **Generic site scrapes** (from Google CSE / RSS follow-through) succeed
  ~40% more reliably with a real browser than with raw httpx.

### How

- Container construction unchanged: `BrowserPool` is only built when
  `LEADS_ENABLE_BROWSER_FETCHER=true`
  ([`container.py:80-88`](../src/container.py#L80-L88)).
- DeepFetch strategies that need the browser check
  `browser_fetcher is not None` at construction time and set
  `is_enabled = False` otherwise. The stage skips disabled strategies with
  a warning, falling back to the no-op path.
- **Recommended default flips in docs** (README + `.env.example` block):
  dev stays `false`, prod example is `true`. Breaking change, but behind a
  flag.
- Browser-based enrichment is still opt-in per source via
  `LEADS_ENRICHMENT_BROWSER_SOURCES=["wellfound","producthunt","linkedin_profile"]`
  (default empty) — avoids spinning up Chromium for every Reddit lead.

### Safeguards

- `BrowserPool.restart_after_pages` (default 100) already handles Chromium
  memory leaks.
- Per-domain rate limiting (§2.5) applies to browser fetches too.
- Browser requests don't retry the full pipeline on failure — they count
  as a soft DeepFetch miss and the pipeline continues with empty content.
  Sufficiency will often trip, which is the right answer for a lead whose
  only content is behind a broken JS wall.

---

## 5. Sufficiency gate (stage 6)

Rule-based, **no LLM call**. Decides whether the accumulated context is
worth sending to SMART classification.

### Default rules (all configurable)

1. `char_count(deep_content + body) < enrichment_sufficiency_min_chars` (default 200) → `insufficient(reason='too_short')`.
2. No `company_domain` AND no person info AND source ∈ `{'google_cse','rss'}` → `insufficient(reason='no_entity')`.
3. Known bot patterns (regex match against author / URL) → `insufficient(reason='bot')`.
4. `source == 'reddit'` AND author has `<5` comments total — when poster
   history is returned by DeepFetch → `insufficient(reason='low_reputation')`.

### Effect

- Sets `raw_leads.status = 'insufficient'`.
- Writes a thin `lead_enrichments` row with only `insufficient_reason`,
  `enriched_at`, and `recommended_approach='skip'`. Other fields NULL.
- Sets `final_score = 0` and **skips Classify + Score**.
- Emits `LeadScored(score=0, recommended_approach='skip')` so downstream
  consumers can treat it uniformly.

### Cost

Zero LLM tokens. One DB write. Protects the classifier budget.

---

## 6. Database schema changes

Existing migrations 001–004 already in place. **New migration 005** (the
enrichment-hardening migration — v1 called this 004, renumbered now that
target_prospects is 004).

```python
# raw_leads — retry state + insufficient status
op.add_column("raw_leads", sa.Column("enrichment_attempts", sa.SmallInteger, server_default="0", nullable=False))
op.add_column("raw_leads", sa.Column("last_enrichment_error_at", sa.DateTime(timezone=True)))

op.drop_constraint("raw_leads_status_check", "raw_leads", type_="check")
op.create_check_constraint(
    "raw_leads_status_check", "raw_leads",
    "status IN ('new','pending_enrichment','enriching','scored',"
    "'enrichment_failed','budget_paused','queued','sent','closed','dead',"
    "'max_attempts_reached','insufficient')",
)

# lead_enrichments — gate audit
op.add_column("lead_enrichments", sa.Column("deep_content_chars", sa.Integer))
op.add_column("lead_enrichments", sa.Column("insufficient_reason", sa.Text))

# company_enrichments — provider attribution + more fields
op.add_column("company_enrichments", sa.Column("industry", sa.Text))
op.add_column("company_enrichments", sa.Column("country", sa.Text))
op.add_column("company_enrichments", sa.Column("provider", sa.Text))

# NEW: person_enrichments — analogous to company_enrichments
op.create_table(
    "person_enrichments",
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("person_key", sa.Text, nullable=False),   # linkedin_url or hash(name+domain)
    sa.Column("title", sa.Text),
    sa.Column("seniority_score", sa.SmallInteger),
    sa.Column("email", sa.Text),
    sa.Column("email_confidence", sa.SmallInteger),
    sa.Column("raw_payload", JSONB, nullable=False),
    sa.Column("provider", sa.Text),
    sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("source", "person_key"),
)
op.create_index("idx_person_enrichments_expires", "person_enrichments", ["expires_at"])

# NEW: enrichment_attempts — per-attempt error log
op.create_table(
    "enrichment_attempts",
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("lead_id", UUID(as_uuid=True), sa.ForeignKey("raw_leads.id", ondelete="CASCADE"), nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("error_class", sa.Text, nullable=False),
    sa.Column("error_message", sa.Text),       # truncated to 1 KB
    sa.Column("attempt", sa.SmallInteger, nullable=False),
    sa.Column("failed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
)
op.create_index("idx_enrichment_attempts_lead", "enrichment_attempts", ["lead_id", sa.text("failed_at DESC")])
```

**Retention**: `enrichment_attempts` rows > 30 days and `llm_call_log` rows
> 90 days are pruned by a daily `HousekeepingWorker`.

---

## 7. Configuration

New settings in [`src/config.py`](../src/config.py), mirrored in
[`.env.example`](../.env.example):

```python
# --- Enrichment retries & rate limits ---
enrichment_max_attempts: int = 3
enrichment_stage_retry_max: int = 3
enrichment_stage_retry_wait_sec: float = 2.0
enrichment_http_per_domain_rps: float = 1.0

# --- DeepFetch ---
enrichment_deepfetch_enabled_sources: list[str] = Field(
    default=["reddit", "hackernews", "google_cse", "rss", "linkedin"],
)
enrichment_deepfetch_max_chars: int = 8000
enrichment_browser_sources: list[str] = Field(default=[])

# --- Sufficiency gate ---
enrichment_sufficiency_min_chars: int = 200
enrichment_sufficiency_min_reddit_karma: int = 5     # 0 disables
enrichment_sufficiency_bot_patterns: list[str] = Field(
    default=[r"^bot[-_]", r"(auto)?reply"],
)

# --- LLM rate limits ---
llm_requests_per_second: float = 2.0
llm_burst: int = 5

# --- Retention ---
enrichment_attempts_retention_days: int = 30
llm_call_log_retention_days: int = 90

# --- Body hygiene ---
enrichment_body_char_limit: int = 4000        # sent to LLM
enrichment_body_store_limit: int = 8000       # stored in raw_leads

# --- Company providers (existing HTTP probe + optional external) ---
enrichment_provider_clearbit_key: str = ""
enrichment_provider_builtwith_key: str = ""

# --- Person providers ---
enrichment_provider_hunter_key: str = ""
enrichment_provider_apollo_key: str = ""
enrichment_linkedin_person_enabled: bool = True  # uses existing LINKEDIN_RAPIDAPI_KEY
```

Browser fetcher itself already has `LEADS_ENABLE_BROWSER_FETCHER` — v2
changes the recommended default in `.env.example` (prod block) to `true`.

---

## 8. Module / file layout

Additive only — no renames. New files marked ⭐.

```
src/modules/enrichment/
├── pipeline.py                       # UPDATED: 9 stages
├── company_resolver.py               # unchanged
├── linkedin_enricher.py              # EXISTS — becomes the linkedin_job DeepFetchStrategy
├── scoring.py                        # add seniority + company_stage weights
├── retry.py                       ⭐ # tenacity policies, shared
├── sanitize.py                    ⭐ # PII redaction (pure)
│
├── deep_fetch/                    ⭐
│   ├── __init__.py                   # registry: source → DeepFetchStrategy
│   ├── base.py                       # Protocol + DeepContent dataclass
│   ├── reddit.py                     # fetches OP post + author history
│   ├── hackernews.py                 # story text + OP profile
│   ├── google_cse.py                 # fetches linked URL via HttpFetcher
│   ├── rss.py                        # same as google_cse
│   ├── linkedin_job.py               # wraps LinkedInJobEnricher
│   ├── linkedin_post.py              # full profile via RapidAPI
│   └── browser.py                    # wellfound / producthunt / linkedin_profile
│
├── providers/                     ⭐
│   ├── __init__.py
│   ├── base.py                       # CompanyDataProvider + PersonDataProvider
│   ├── company_http_probe.py         # extracted from current EnrichCompanyStage
│   ├── company_clearbit.py           # optional
│   ├── company_builtwith.py          # optional
│   ├── person_linkedin.py            # uses LINKEDIN_RAPIDAPI_KEY
│   ├── person_hunter.py              # optional
│   └── person_apollo.py              # optional
│
└── stages/
    ├── fetch.py                      # existing
    ├── deep_fetch.py              ⭐ # picks strategy per lead.source
    ├── resolve_company.py            # existing; now reads deep_content
    ├── enrich_company.py             # refactored to provider chain
    ├── enrich_person.py           ⭐ # provider chain
    ├── sufficiency.py             ⭐ # rule-based gate
    ├── classify.py                   # existing; reads deep_content
    ├── score.py                      # existing
    └── persist.py                    # also writes insufficient_reason

src/infrastructure/
├── rate_limit.py                  ⭐ # async token bucket
└── postgres_repo.py                  # + person_enrichments, enrichment_attempts tables

src/application/
└── workers.py                        # + HousekeepingWorker

src/domain/
├── models.py                         # + DeepContent
└── interfaces.py                     # + DeepFetchStrategy, PersonDataProvider, CompanyDataProvider
```

---

## 9. Sensitive / excessive data handling

1. **Trim at the gate**: scraper normalize trims `body` to
   `enrichment_body_store_limit`. Already done in the LinkedIn adapter at
   [`linkedin.py:186`](../src/modules/scraping/adapters/linkedin.py).
2. **Trim again at the prompt**: the classifier uses at most
   `enrichment_body_char_limit` chars; DeepFetch output trimmed to
   `enrichment_deepfetch_max_chars`.
3. **PII redaction**: conservative regex for emails / phone numbers, replace
   with `<email>` / `<phone>` before the LLM sees the text. Central pure
   function in `modules/enrichment/sanitize.py`.
4. **Don't persist prompts or raw responses.** Only validated
   Pydantic-modelled scalars.
5. **HTML from DeepFetch**: never stored. Parsed in-memory; only extracted
   text + char count persists.
6. **`llm_call_log`**: token counts + cost only — no prompt / completion.
   Already true today; keep it.
7. **Person data**: only store what's needed for scoring and outreach
   (`title`, `seniority_score`, `email`, `email_confidence`). Don't store
   the full profile JSON — keep it in `raw_payload` behind the TTL.
8. **`person_enrichments.expires_at`**: default 60 days. People change
   jobs; stale data is worse than no data for outreach.

---

## 10. Observability

- Every stage uses `structlog.bind(lead_id=..., stage=..., attempt=...)`.
- New route `GET /api/v1/health/enrichment` extended to include:
  - `EnrichmentWorker.stats`
  - Resweeper last-run timestamp
  - `insufficient` count in last hour
  - `enrichment_failed` count in last hour
  - `get_daily_llm_cost()`
  - Per-provider call counts (from `llm_call_log` aggregation and in-memory
    counters on each provider).
- Add `stage_duration_ms` to pipeline context; each stage records on exit.

---

## 11. Step-by-step implementation plan

Small PRs, approximately one per step. **Bold** = minimum to flip
`LEADS_ENABLE_ENRICHMENT=true` in staging with confidence.

1. **Migration 005** (`enrichment_attempts`, `person_enrichments`,
   `insufficient` status, audit columns).
2. **`modules/enrichment/retry.py`** (tenacity policies). Wire into current
   Classify + Resolve + EnrichCompany.
3. **Failure path**: repository `record_attempt()`, worker writes
   `enrichment_failed`, resweeper promotes to `max_attempts_reached`.
4. **DeepFetch Protocol + `DeepFetchStage` + noop strategy.** Pipeline
   becomes 7 stages. No strategies implemented yet — graceful degradation.
5. **DeepFetch strategies (batch 1)**: `reddit`, `hackernews`, `google_cse`,
   `rss`. Pure HTTP — no browser needed.
6. **Sufficiency stage**: rule-based, config-driven thresholds. Wires
   `insufficient` status.
7. **Provider chain refactor**: extract current HEAD+title logic into
   `providers/company_http_probe.py`; introduce `CompanyDataProvider`;
   `EnrichCompanyStage` iterates the chain.
8. **PersonDataProvider Protocol + `EnrichPersonStage`**: wire
   `person_linkedin` (reuses existing RapidAPI key). Other providers added
   later as keys appear.
9. **DeepFetch strategies (batch 2)**: `linkedin_job` (wraps existing
   `LinkedInJobEnricher`), `linkedin_post` (full-profile fetch).
10. **Browser-backed DeepFetch**: `wellfound`, `producthunt`,
    `linkedin_profile`. Flip `.env.example` prod block to
    `LEADS_ENABLE_BROWSER_FETCHER=true`.
11. **Rate limiting**: `infrastructure/rate_limit.py`; wrap LLM providers
    and every `DeepFetchStrategy` / `CompanyDataProvider` /
    `PersonDataProvider`.
12. **Body hygiene**: enforce `enrichment_body_store_limit` in all
    adapters' normalize; `sanitize.py` PII redaction; apply in Classify.
13. **Housekeeping**: `HousekeepingWorker` daily prune.
14. **Scoring weight update**: add `seniority_score` and `company_stage`.
    Changelog entry required — this changes every score.
15. **Observability endpoint**: extend `/health/enrichment`.
16. **Docs**: `docs/CHANGELOG.md`, `.env.example`, README enrichment
    section.

Steps 1–6 unblock staging. Steps 7–10 deliver the value described in §2.
Steps 11–16 are production-hardening and polish.

---

## 12. Test plan

Existing suite (`test_enrichment_stages.py`, `test_enrichment_integration.py`)
stays green — stages keep their public contract. Extensions:

**Unit**
- `test_scoring.py` (exists): new cases for `seniority_score` and
  `company_stage` weights; skip-penalty cap unchanged.
- `test_sanitize.py` ⭐: email/phone redaction.
- `test_rate_limit.py` ⭐: token bucket burst + refill.
- `test_retry.py` ⭐: retry on `TransportError` + 5xx, not on 401.
- `test_deep_fetch_*.py` ⭐: one per strategy — all with mocked httpx/browser.
- `test_providers_company.py` ⭐: chain short-circuits, merges, handles all-fail.
- `test_providers_person.py` ⭐: same pattern.
- `test_sufficiency.py` ⭐: each rule in isolation + combined scenarios.

**Integration** (testcontainers Postgres)
- **Happy path** for every source: `new` → `scored`, with DeepFetch
  content visible in `lead_enrichments.deep_content_chars`.
- **Insufficient path**: 50-char Reddit title → `insufficient`, no LLM call,
  no `llm_call_log` row, score=0.
- **Failure path**: LLM raises → `enrichment_failed`, attempt row written,
  third failure → `max_attempts_reached`.
- **Budget exceeded mid-run** → `budget_paused`.
- **Resweeper**: lead stuck in `enriching` → re-published.
- **Browser-disabled parity**: enrichment still succeeds with empty content
  for browser-only sources; they score low via Sufficiency, not via error.
- **Provider graceful degradation**: no person keys configured → pipeline
  completes, no crash, `person_*` fields NULL.

**Contract**
- With `LEADS_ENABLE_ENRICHMENT=false`: no workers, no LLM provider in
  container, ingestion still writes `status='new'`. Regression guard.

**Load** (manual)
- 1000 synthetic leads across sources, mock LLM + mock providers.
- Verify: concurrency respects `max_concurrent_enrichments`, queue drops
  recover via resweeper, browser-pool memory stable after 1000 pages,
  circuit breaker trips + recovers, budget gate halts further SMART calls.

---

## 13. Risks, open questions, tradeoffs

### Risks
- **LLM cost variance.** DeepFetch inflates prompt size by 5–20×. Budget
  ceiling enforces the dollar cap; per-stage `max_output_tokens` cap
  prevents runaway outputs.
- **Browser pool as a single point of failure in prod.** Recycle-after-N
  helps, but a hung Chromium can block the pool. Mitigation: per-strategy
  timeout of `browser_page_timeout_sec * 1.5`, and DeepFetch treats timeouts
  as soft misses, not fatal errors.
- **Per-person API abuse exposure.** Hunter / Apollo bill per lookup; the
  token bucket enforces RPS but not daily budget. Add
  `person_daily_lookup_budget` later.
- **Cache staleness**: `person_enrichments.expires_at = 60d` is a guess.
  People change jobs. Shorten to 30d if score drift becomes an issue.
- **Stage 4 / 5 fan-out**: each provider call is sequential in the chain.
  If the chain grows past 3 providers, revisit for parallelism — but only
  if the p95 latency is actually a problem.

### Open questions
- Should Sufficiency *also* block DeepFetch (cheap-gate before spending
  network)? Current design runs DeepFetch first so the gate can see the
  fetched content. Cheap-gate first would save bandwidth but add false
  negatives. Revisit after measuring.
- Should `target_companies` / `target_people` rows ever *seed* new
  `raw_leads`? Cleaner to keep them separate for now.
- When `LEADS_ENABLE_ENRICHMENT=true` but `LEADS_ENABLE_BROWSER_FETCHER=false`,
  do we hard-fail at startup or warn? Current plan: warn, run degraded.
  Flip if this bites us in ops.

### Tradeoffs accepted
- **In-process bus, not Kafka.** Durable queue = Postgres. Revisit only at
  multi-node scale (see project memory).
- **Pure-Python scoring, not a learned model.** Auditable, tunable, no
  training-data dependency. Revisit when there's conversion data worth
  learning from.
- **Rule-based Sufficiency, not a model.** Zero LLM cost, deterministic.
  Good leads occasionally get gated; bad leads very rarely slip through.
  Tune thresholds from ops metrics, not retraining.
- **No intra-stage parallelism.** Provider chains are sequential. Saves
  complexity; costs 200–800 ms per lead. Not worth optimising at current
  volume.
- **Browser fetching promoted, but still optional.** Dev stays lean; prod
  operators opt in. Sharper than the v1 "optional extra" framing.

---

## Appendix A — Files referenced

| Concern | File |
|---|---|
| App factory & lifespan | [`src/main.py`](../src/main.py) |
| DI wiring | [`src/container.py`](../src/container.py) |
| Settings | [`src/config.py`](../src/config.py) |
| Domain models | [`src/domain/models.py`](../src/domain/models.py) |
| Protocols | [`src/domain/interfaces.py`](../src/domain/interfaces.py) |
| Events | [`src/domain/events.py`](../src/domain/events.py) |
| Event bus | [`src/application/bus.py`](../src/application/bus.py) |
| Workers | [`src/application/workers.py`](../src/application/workers.py) |
| Pipeline | [`src/modules/enrichment/pipeline.py`](../src/modules/enrichment/pipeline.py) |
| Stages | [`src/modules/enrichment/stages/`](../src/modules/enrichment/stages/) |
| Scoring | [`src/modules/enrichment/scoring.py`](../src/modules/enrichment/scoring.py) |
| Company resolver | [`src/modules/enrichment/company_resolver.py`](../src/modules/enrichment/company_resolver.py) |
| LinkedIn job enricher | [`src/modules/enrichment/linkedin_enricher.py`](../src/modules/enrichment/linkedin_enricher.py) |
| Repository | [`src/infrastructure/postgres_repo.py`](../src/infrastructure/postgres_repo.py) |
| LLM providers | [`src/infrastructure/anthropic_provider.py`](../src/infrastructure/anthropic_provider.py), [`src/infrastructure/openai_provider.py`](../src/infrastructure/openai_provider.py) |
| Fetchers | [`src/infrastructure/fetchers/`](../src/infrastructure/fetchers/) |
| Browser fetcher | [`src/infrastructure/fetchers/browser.py`](../src/infrastructure/fetchers/browser.py) |
| Prompts | [`src/prompts/`](../src/prompts/) |
| Migrations | [`alembic/versions/`](../alembic/versions/) |
