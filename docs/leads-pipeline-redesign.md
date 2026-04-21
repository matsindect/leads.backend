# Leads Pipeline — Architecture Audit & Redesign

**Status:** Draft for review
**Scope:** whole system — discovery, domain model, enrichment, scoring, persistence
**Companion:** [enrichment-pipeline-design.md](./enrichment-pipeline-design.md) (stage-level design). This doc zooms out to the system shape; the enrichment doc stays authoritative for pipeline stage mechanics.

This document audits the current codebase against a "high-quality ICP-driven
B2B leads pipeline" target, and proposes an incremental redesign. Every
recommendation cites the current implementation it builds on — nothing in
here is a rewrite-from-scratch proposal.

---

## 1. Current architecture summary

### 1.1 Topology

- **Modular monolith.** One FastAPI process, two logical modules: scraping
  ([`src/modules/scraping/`](../src/modules/scraping/)) and enrichment
  ([`src/modules/enrichment/`](../src/modules/enrichment/)).
- **Postgres-as-queue.** `raw_leads.status` is the durable queue; the
  [`EventBus`](../src/application/bus.py) is an opportunistic fast path; the
  [`PendingLeadsResweeper`](../src/application/workers.py#L111-L163) is the
  safety net.
- **Single shared repository class** ([`PostgresLeadRepository`](../src/infrastructure/postgres_repo.py))
  satisfying `LeadRepository`, `EnrichmentRepository`, and now
  `ProspectRepository`.
- **DI via `Container`** ([`src/container.py`](../src/container.py)) attached
  to `app.state` during lifespan startup. No `dependency-injector` library.

### 1.2 How leads get in today

The scraper fleet is 10 adapters under
[`modules/scraping/adapters/`](../src/modules/scraping/adapters/) (Reddit,
HackerNews, HN-Hiring, Wellfound, ProductHunt, RSS-multi, Google CSE,
LinkedIn/RapidAPI, funding, RemoteOK), all conforming to
[`SourceAdapter`](../src/domain/interfaces.py#L27-L71).

[`ScrapeOrchestrator.run()`](../src/modules/scraping/orchestrator.py#L37-L128):
`fetch_raw → normalize → insert_leads (ON CONFLICT) → publish LeadCreated`.
Circuit-broken off `scrape_runs` history.

**Queries are env-configured, not ICP-derived:**
- [`config.py:59-114`](../src/config.py#L59-L114) —
  `reddit_subreddits=["startups","SaaS","webdev"]`,
  `linkedin_job_queries=["python developer",...]`,
  `google_cse_queries=["site:reddit.com hiring developer",...]`,
  `rss_feed_urls=[]`, etc.
- Per-request override is possible via
  [`ScrapeRequest`](../src/api/schemas.py#L29-L87) on
  `POST /scrape/{adapter_name}`, but there's no persistent ICP object —
  every caller has to know the shape.

**Signal classification is regex-first:**
[`modules/scraping/signals.py`](../src/modules/scraping/signals.py) has a
dev-focused pattern table (9 signal types) with keyword extraction.
Per-request `signal_patterns` can override. Classification runs in the
adapter's `normalize()` — it's pre-insert, not a separate stage.

**Prospect discovery (just added):** `POST /prospects/linkedin/companies`
and `POST /prospects/linkedin/employees`
([`api/routes.py`](../src/api/routes.py)) feed `target_companies` and
`target_people` — these tables are disconnected from `raw_leads`.

### 1.3 How leads get scored today

Six-stage enrichment pipeline
([`EnrichmentPipeline`](../src/modules/enrichment/pipeline.py#L31-L75)),
opt-in via `LEADS_ENABLE_ENRICHMENT`:

1. `FetchStage` — claim + idempotency
2. `ResolveCompanyStage` — cheap LLM → company_name/domain
3. `EnrichCompanyStage` — HTTP HEAD + title scrape
4. `ClassifyStage` — SMART LLM → signal/ICP/urgency/stack/decision_maker
5. `ScoreStage` — pure [`compute_final_score()`](../src/modules/enrichment/scoring.py#L15-L56)
6. `PersistStage` — upsert + emit `LeadScored`

Scoring weights ([`scoring.py:27-32`](../src/modules/enrichment/scoring.py#L27-L32)):
`signal 0.20 / icp 0.25 / urgency 0.15 / decision_maker 0.15 / stack 0.15 / recency 0.10`.
`user_skills` from env drives stack-match.

### 1.4 Persistence (tables exist today)

| Table | Purpose | Key |
|---|---|---|
| `raw_leads` | every discovered item | UUID; unique `(source, source_id)` + `dedup_hash` |
| `scrape_runs` | per-adapter run audit | UUID |
| `lead_enrichments` | LLM output per lead | `lead_id` FK |
| `company_enrichments` | per-domain company data | `domain` |
| `company_resolutions` | LLM resolver cache | `cache_key` |
| `llm_call_log` | cost + usage audit | bigint |
| `target_companies` | LinkedIn company discovery | UUID; unique `(source, source_id)` |
| `target_people` | LinkedIn employee discovery | UUID; unique `(source, source_id)` |

### 1.5 Cross-cutting concerns already in place

- Retry/backoff in [`HttpFetcher._fetch`](../src/infrastructure/fetchers/http.py#L96-L212)
  (manual loop, not tenacity).
- Rate-limit header parsing (`x-ratelimit-*`, `Retry-After`).
- Circuit breaker per-adapter via `scrape_runs` history
  ([`orchestrator.py:130-144`](../src/modules/scraping/orchestrator.py#L130-L144)).
- LLM provider abstraction:
  [`LLMProvider` Protocol](../src/domain/interfaces.py#L151-L162) +
  `ModelHint.CHEAP|SMART`. Anthropic + OpenAI implementations.
- Daily LLM budget ceiling → `status='budget_paused'`.
- Structlog JSON logging.
- Graceful shutdown with `SIGTERM` handler and bounded worker wait.
- Feature flags: `LEADS_ENABLE_ENRICHMENT`, `LEADS_ENABLE_BROWSER_FETCHER`.

---

## 2. Gap analysis against the target pipeline

Specific, tied to current files.

### 2.1 ICP-driven search — **largely missing**

| Requirement | Current state |
|---|---|
| Start from an explicit ICP | ❌ No ICP entity. `user_skills` in env is the closest thing. |
| Industry / size / geography / tech filters | ⚠️ Partial: LinkedIn `/search-companies` just added supports industry codes, headcount, tech, HQ geo. No other adapter does. |
| Job titles / seniority filters | ❌ Reddit / HN / Google CSE scrape broad text, not role-filtered. |
| Hiring signals | ⚠️ Regex patterns in [`signals.py:27-45`](../src/modules/scraping/signals.py#L27-L45). Fires on title text, not verified hiring pages. |
| Growth signals | ⚠️ Regex-only (`\b(expand\|scale\|growing)\b`). No funding-round deduplication, no headcount growth tracking. |
| Buying intent | ⚠️ Regex only. Bintent signals (intent data providers, site-visit triggers) not modelled. |
| Query generation from ICP | ❌ Queries are static env lists per adapter. |

**Concrete gap**: there is no object that says *"this is the customer I'm
looking for"*. Every adapter's query set is independent, hand-curated, and
unaware of the others.

### 2.2 Account / contact / signal separation — **missing**

| Target entity | Today |
|---|---|
| `Account` (normalized company) | ❌ `raw_leads.company_name` (raw string), `raw_leads.company_domain` (raw string). No dedup across leads, no canonical record. |
| `Contact` (normalized person) | ❌ `raw_leads.person_name` / `person_role` (raw strings). |
| `Signal` (dated event) | ⚠️ `raw_leads` doubles as a signal log but also holds source content, company fields, person fields, enrichment status — mixing four concerns in one row. |
| `EnrichmentRun` (attempt audit) | ❌ Only aggregate `scrape_runs`. No per-lead enrichment attempt log. |
| `DataSource` registry | ❌ Sources are adapter class names + hardcoded enum-like `source` string on `raw_leads`. |
| `ProviderResult` (per-field provider output) | ❌ `company_enrichments` columns are direct scalars — no provenance. |

**Concrete gap**: "Acme" in a Reddit post, "Acme Inc." on a LinkedIn job,
and `acme.io` in a funding article become three unrelated `raw_leads` rows.
No query answers *"show me all signals for Acme"*.

### 2.3 Enrichment quality — **early and shallow**

- Company enrichment = `HEAD` + HTML `<title>` regex
  ([`enrich_company.py:52-84`](../src/modules/enrichment/stages/enrich_company.py#L52-L84)).
- No provider abstraction — no Clearbit/Apollo/BuiltWith/Hunter hooks.
- No contact enrichment at all.
- No email verification.
- No tech-stack detection beyond the LLM guessing from post text.
- `LinkedInJobEnricher` just built but not yet plugged in.

### 2.4 Deduplication — **lead-level only**

[`modules/scraping/dedup.py:19-34`](../src/modules/scraping/dedup.py#L19-L34)
hashes on `(company_domain | person_name | url, signal_type, day_bucket)`.
This is dedup-by-event, not by-account or by-contact. The same company
reposting hiring signals on 5 days produces 5 rows; that's a feature for
signals but it means there's no "merge these leads because they're about
the same account" logic.

### 2.5 Normalization — **almost none**

- Company names: adapters extract whatever the source returns.
  "Acme Inc", "acme", "Acme, Inc." all live.
- Domains: `_to_target_company` in [`linkedin.py`](../src/modules/scraping/adapters/linkedin.py)
  takes `raw.get("website") or raw.get("domain")` verbatim — no
  `https://` stripping, no `www.` stripping.
- Titles/roles: free-text, no normalized ladder ("Senior Engineer" vs
  "Sr. Eng.").
- Industries: LinkedIn returns industry codes as ints, no mapping to a
  stable taxonomy.
- Countries / locations: free-text ("SF", "San Francisco, CA", "Bay Area").

### 2.6 Confidence scoring per field — **missing**

The closest thing today is `lead_enrichments.refined_signal_strength`
(0-100), which is the LLM's own self-reported confidence for the whole
classification. No per-field confidence anywhere.

### 2.7 Source attribution per field — **missing**

`raw_leads.source` records *which adapter found the row*, but once
`company_enrichments.employee_count` is filled, there's no way to know
whether that came from Clearbit, BuiltWith, or a web probe. My previously-
proposed `company_enrichments.provider TEXT` column would cover *row-level*
attribution; *field-level* attribution needs a `provider_results` table.

### 2.8 Freshness — **row-level only**

`company_enrichments.expires_at` ages the whole row out at 30 days. If
`employee_count` is 11 months old but `funding_stage` was just fetched,
we refresh both together or neither. Acceptable today; a blocker once
multiple providers contribute to the same row.

### 2.9 Provider fallback — **not modelled**

No chain, no fallback. `EnrichCompanyStage` has one strategy.

### 2.10 Email verification — **missing end-to-end**

No email field on any entity. No verification provider hook.

### 2.11 Async jobs + production controls — **mostly present, partial gaps**

| Control | Current |
|---|---|
| Async enrichment jobs | ✅ [`EnrichmentWorker`](../src/application/workers.py#L34-L108) |
| Queueing | ✅ EventBus + Postgres status column |
| Retry policies | ⚠️ HTTP fetcher retries, but no stage-level retries. Enrichment pipeline has no per-stage retry. |
| Rate limits | ⚠️ HTTP fetcher parses headers but doesn't throttle proactively. No per-provider RPS caps. |
| Provider budgets | ⚠️ Daily LLM USD cap via `get_daily_llm_cost()`. Per-provider (Hunter/Apollo) not modelled. Google CSE has a query-count budget ([`google_cse_daily_query_budget`](../src/config.py#L97)) — not generalised. |
| Idempotency | ✅ `FetchStage` claims via status change; terminal statuses short-circuit. |
| Circuit breakers | ✅ per-adapter scraping + consecutive-failure worker pause |
| Observability | ⚠️ `EnrichmentWorker.stats` exists but exposed only via `/health/enrichment` (recently added); no per-provider counters. |
| Structured logging | ✅ structlog JSON |
| Graceful failure | ⚠️ Exceptions caught in worker; no `enrichment_failed` status write today. |
| Feature flags | ✅ `LEADS_ENABLE_ENRICHMENT`, `LEADS_ENABLE_BROWSER_FETCHER` |

---

## 3. Proposed domain model

Shift from **lead-centric** (`raw_leads` = everything) to **entity-centric**
(accounts + contacts + signals + enrichment audit). `raw_leads` is preserved
as the **signal inbox**; every row gets resolved into the entity graph after
insertion, not replaced.

### 3.1 Entity overview

```
                ┌────────────────┐
                │   ICPProfile   │
                └───────┬────────┘
                        │ (drives source queries)
                        ▼
┌──────────────┐   ┌──────────┐   ┌─────────┐
│  DataSource  │──▶│ raw_leads│──▶│ Signal  │
└──────────────┘   │ (inbox)  │   └────┬────┘
                   └──────────┘        │
                                       ▼
                              ┌──────────────────┐
                              │     Account      │◀──┐
                              └──────┬───────────┘   │
                                     │ 1-M           │
                                     ▼               │
                              ┌──────────────────┐   │
                              │     Contact      │───┘ (Contact.account_id)
                              └──────┬───────────┘
                                     │
                                     ▼
                  ┌──────────────────┴──────────────────┐
                  │                                     │
         ┌────────▼───────┐                    ┌────────▼──────┐
         │AccountEnrichmt │                    │ContactEnrichmt│
         │   (per field)  │                    │  (per field)  │
         └────────┬───────┘                    └────────┬──────┘
                  │                                     │
                  ▼                                     ▼
          ┌──────────────┐                      ┌──────────────┐
          │ ProviderResult (one row per (entity, field, provider))│
          └────────────────────────────────────────────────────────┘

                       ┌──────────────────┐
                       │  EnrichmentRun   │ (attempt audit, per stage × per lead)
                       └──────────────────┘
```

### 3.2 Entities

#### `ICPProfile`
Persisted definition of what customer we're hunting. One or many per tenant.
- `id`, `name`, `owner` (user key; single-tenant today so optional)
- **Firmographics**: `industries[]`, `headcount_bands[]`,
  `countries[]`, `annual_revenue_range`
- **Technographics**: `required_stack[]`, `avoided_stack[]`
- **Signals**: `signal_types_of_interest[]`, `min_signal_strength`
- **Titles**: `target_titles[]`, `target_seniorities[]`
- **Query hints**: `keyword_seeds[]`, `excluded_keywords[]`
- `created_at`, `updated_at`, `active bool`
- **Why**: today's queries are env lists. Promoting them to a row lets the
  same ICP drive multiple adapters consistently and evolve without deploys.

#### `DataSource`
Registry of every source that can contribute signals. Seeded once.
- `id TEXT PK` (`'reddit'`, `'linkedin_job'`, `'hackernews'`, `'google_cse'`, …)
- `kind ENUM` (`'scraper'`, `'enrichment_provider'`, `'search'`)
- `trust_score SMALLINT` (0–100) — feeds confidence propagation
- `requires_browser BOOL`
- **Why**: today `raw_leads.source` is a free-form string; without a
  registry, there's nothing to join confidence or trust to.

#### `Account`
Canonical company record, deduplicated across sources.
- `id UUID PK`
- `canonical_domain TEXT` (normalized: lowercase, no `www.`, no scheme)
  — primary dedup key; unique
- `canonical_name TEXT`
- `name_aliases TEXT[]` (all seen forms)
- `industry TEXT`, `sub_industry TEXT`
- `headcount_band TEXT` (canonical: `1-10`, `11-50`, …)
- `employee_count INT`, `employee_count_source TEXT`
- `annual_revenue_min`, `annual_revenue_max`, `annual_revenue_currency`
- `country TEXT` (ISO-3166 alpha-2), `region TEXT`, `city TEXT`
- `funding_stage TEXT`, `last_funded_at DATE`, `total_funding_usd BIGINT`
- `tech_stack TEXT[]` (merged across signals + providers)
- `linkedin_url TEXT`, `crunchbase_url TEXT`
- `first_seen_at TIMESTAMPTZ`, `last_seen_at TIMESTAMPTZ`
- `last_enriched_at TIMESTAMPTZ`
- **Why**: today a company mentioned in 10 signals is 10 string values.
  One account row = one source of truth for firmographics.

#### `Contact`
Canonical person record, deduplicated.
- `id UUID PK`
- `account_id UUID FK` (nullable — sometimes we don't know the company)
- `full_name TEXT`, `first_name TEXT`, `last_name TEXT`
- `linkedin_url TEXT UNIQUE` (primary dedup when present)
- `email TEXT`, `email_status ENUM('unverified','valid','invalid','catch_all','unknown')`,
  `email_verified_at TIMESTAMPTZ`
- `title TEXT`, `title_normalized TEXT`
- `seniority TEXT` (`ic`, `senior_ic`, `manager`, `director`, `vp`, `c_level`)
- `location TEXT`, `country TEXT`
- `first_seen_at`, `last_seen_at`, `last_enriched_at`
- **Why**: today we store `person_name TEXT` on `raw_leads` and that's it.
  Outreach needs verified email + role; qualification needs seniority.

#### `Signal`
A timestamped *event* tied to an account and/or contact. Replaces
`raw_leads` as the canonical "buying-intent" record.
- `id UUID PK`
- `account_id UUID FK NULL`, `contact_id UUID FK NULL`
- `signal_type ENUM` (reuse existing `SignalType`)
- `signal_strength SMALLINT` (0–100) — source-attributed, not the final
  score
- `observed_at TIMESTAMPTZ`, `ingested_at TIMESTAMPTZ`
- `source_id TEXT FK → data_sources.id`
- `source_url TEXT` — link back to the original post/article
- `raw_lead_id UUID FK → raw_leads.id NULL` — preserves the inbox link
- `dedup_key TEXT UNIQUE` (generalised dedup.py scheme; see §6)
- **Why**: separates "what was said" from "who said it" (contact) and
  "about whom" (account). Enables queries like *"all hiring signals for
  accounts in SaaS this week."*

#### `AccountEnrichment` / `ContactEnrichment`
Field-level audit, not a blob. One row per (entity, field, provider).
- `id BIGSERIAL PK`
- `account_id` (or `contact_id`) `UUID FK`
- `field_name TEXT` (`'employee_count'`, `'title'`, …)
- `field_value JSONB` (scalar wrapped for uniform shape)
- `provider TEXT FK → data_sources.id`
- `confidence SMALLINT` (0–100)
- `observed_at TIMESTAMPTZ`, `expires_at TIMESTAMPTZ`
- **Why**: lets us answer *"where did this value come from and when?"*
  and pick the winning value when two providers disagree (see §6).

The `Account` / `Contact` row carries the **currently-active** value for
each field (fast read); the enrichment table is the **history/audit**
(slow, but complete provenance). Writes go to both atomically.

#### `EnrichmentRun`
Per-attempt audit for every enrichment stage.
- `id BIGSERIAL PK`
- `lead_id UUID FK` (or `account_id` / `contact_id` for entity-level runs)
- `stage TEXT`, `provider TEXT`
- `status ENUM('ok','empty','error','rate_limited','timeout','skipped')`
- `error_class TEXT`, `error_message TEXT` (truncated 1 KB)
- `duration_ms INT`
- `input_tokens INT`, `output_tokens INT`, `cost_usd NUMERIC(10,6)`
- `run_at TIMESTAMPTZ`
- **Why**: today `llm_call_log` audits LLM calls only. This generalises to
  every provider + stage and becomes the debugging table.

#### `OutreachContext` (optional, later phase)
Personalization payload materialised once per contact per campaign.
- `id`, `contact_id`, `campaign_id`, `generated_at`
- `pain_summary`, `opener_hook`, `recent_trigger_summary`
- `ttl_expires_at`
- **Why**: separates *qualification* (Account + Contact enrichment) from
  *outreach-ready content* (a SMART-model output, ephemeral, re-generable).

### 3.3 What `raw_leads` becomes

Kept as the **immutable inbox**. Each row is still a discovered post/page.
Gains two FKs:
- `account_id UUID NULL` (populated by the resolver stage)
- `contact_id UUID NULL` (populated by the person-resolver stage)

The existing status machine (`new → enriching → scored / insufficient /
enrichment_failed / …`) unchanged. This preserves the resweeper invariant
and the ingestion-when-disabled contract.

---

## 4. Proposed pipeline design

Two pipelines, decoupled by the Signal → Account/Contact resolver:

### 4.1 Discovery pipeline (sync per request, async per poll)

```
ICPProfile
   │
   ▼
QueryPlanner (per source) ──▶ per-adapter ScrapeRequest
   │
   ▼
ScrapeOrchestrator (existing) ──▶ raw_leads (inbox) ──▶ emit LeadCreated
```

- **QueryPlanner** (new, `modules/scraping/query_planner.py`): ICPProfile
  → `dict[adapter_name, ScrapeRequest]`. Translates ICP fields to each
  adapter's accepted filter keys
  ([`AdapterParamSchema`](../src/api/schemas.py#L90-L106)).
  E.g. ICP industries → LinkedIn industry codes via a static map; ICP
  stack → Google CSE queries like `site:reddit.com "python" "hiring"`.
- Scraper orchestration **unchanged** — adapters already take
  `ScrapeRequest`. The planner just populates it.
- API: `POST /icp/{id}/scrape` triggers all adapters with planner-generated
  requests. Replaces hand-rolling env queries.
- Manual `POST /scrape/{adapter}` stays (backwards-compat).

### 4.2 Resolution + enrichment pipeline (async)

Extends the 9-stage pipeline in
[enrichment-pipeline-design.md](./enrichment-pipeline-design.md) with
explicit entity resolution steps. Stages that produce or mutate entities
are marked 🏛️.

```
Fetch (raw_leads row)
  │
  ▼
DeepFetch         — pull richer content per source
  │
  ▼
🏛️ ResolveAccount  — dedup/normalize into Account row, write raw_leads.account_id
  │
  ▼
🏛️ ResolveContact  — dedup/normalize into Contact row, write raw_leads.contact_id
  │
  ▼
EnrichAccount     — CompanyDataProvider chain, write Account + AccountEnrichment rows
  │
  ▼
EnrichContact     — PersonDataProvider + EmailVerificationProvider chains
  │
  ▼
🏛️ RecordSignal    — create Signal row linking account+contact with signal_type
  │
  ▼
Sufficiency       — rule-based gate; sets status=insufficient when thin
  │
  ▼
Classify (SMART)  — full context now: Account + Contact + Signal + DeepContent
  │
  ▼
Score             — pure function, account+contact+signal weighted (see §9)
  │
  ▼
Persist           — update raw_leads.status, upsert lead_enrichments, emit LeadScored
```

- **Resolve** stages replace the current `ResolveCompanyStage`. They
  produce FK links to normalized entities, not loose strings.
- **EnrichAccount / EnrichContact** replace the current EnrichCompanyStage
  and cover the missing person enrichment.
- **RecordSignal** is the row that downstream "filter accounts with
  hiring signals in last 14 days" queries read from.

### 4.3 Sync vs async boundary

| Operation | Sync | Async |
|---|---|---|
| `POST /scrape/{adapter}` | ✅ returns RunReport | — |
| `POST /icp/{id}/scrape` | ✅ returns per-adapter RunReports | — |
| Scheduled adapter polls | — | ✅ background loop |
| Enrichment pipeline | — | ✅ EventBus → EnrichmentWorker |
| Email verification | — | ✅ batched (dedicated worker) |
| `GET /accounts/{id}` | ✅ | — |
| `GET /signals` | ✅ | — |
| Re-score on weight change | — | ✅ resweeper-style |

Ingestion never awaits enrichment — preserves the "disable enrichment,
scraping still works" contract.

---

## 5. Enrichment provider architecture

Extends §3 of [enrichment-pipeline-design.md](./enrichment-pipeline-design.md).
Three Protocols, each independently disable-able:

### 5.1 `CompanyDataProvider`

```python
class CompanyDataProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def is_enabled(self) -> bool: ...
    @property
    def provides_fields(self) -> frozenset[str]: ...  # ('employee_count', 'industry', …)
    @property
    def cost_per_call_usd(self) -> float: ...
    async def enrich(self, domain: str) -> ProviderResult | None: ...
```

Where `ProviderResult` is:
```python
@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    fields: dict[str, Any]                    # scalar values only
    confidences: dict[str, int]               # 0-100 per field
    observed_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)  # kept in audit, not stored on Account
```

Concrete providers:
- `HttpProbeProvider` (extract existing HEAD+title logic from
  [`enrich_company.py`](../src/modules/enrichment/stages/enrich_company.py)).
  Fields: `is_reachable`, `homepage_title`. Confidence 60.
- `ClearbitProvider` — fields: employee_count, industry, headquarters,
  funding_stage. Confidence 85.
- `BuiltWithProvider` — fields: tech_stack. Confidence 75.
- `LinkedInCompanyProvider` — uses existing RapidAPI key; fields:
  headcount, industry, linkedin_url. Confidence 80.

### 5.2 `ContactDataProvider`

```python
class ContactDataProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def is_enabled(self) -> bool: ...
    @property
    def provides_fields(self) -> frozenset[str]: ...
    async def enrich(self, *,
        linkedin_url: str | None,
        full_name: str | None,
        company_domain: str | None,
    ) -> ProviderResult | None: ...
```

Concrete providers:
- `LinkedInContactProvider` — full profile via existing RapidAPI;
  fields: title, seniority, location. Confidence 80.
- `HunterContactProvider` — fields: email, email_confidence. Confidence 70.
- `ApolloContactProvider` — fields: title, seniority, email. Confidence 75.

### 5.3 `EmailVerificationProvider`

```python
class EmailVerificationProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def is_enabled(self) -> bool: ...
    async def verify(self, email: str) -> EmailVerificationResult: ...

@dataclass(frozen=True, slots=True)
class EmailVerificationResult:
    email: str
    status: Literal['valid','invalid','catch_all','unknown']
    confidence: int
    verified_at: datetime
```

Concrete providers: `HunterVerifier`, `ZeroBounceVerifier`, `NeverBounceVerifier`.
Separate from `ContactDataProvider` because verification is a distinct API
call with different rate limits / cost / SLA — bundling them hides the
cost model.

### 5.4 `TechStackDetector` (specialised)

Lives in the company-provider group but called out because it often has
purpose-built APIs (`BuiltWith`, `Wappalyzer`, `StackShare`). Returns
`ProviderResult` with field `tech_stack: list[str]`, plus confidence
per-tech when the provider reports it.

### 5.5 `SignalProvider` (for external intent data)

Future-proofing: some pipelines integrate Bombora / G2 / Clearbit Reveal.
Not in scope for v1, but the Protocol shape is:

```python
class SignalProvider(Protocol):
    async def recent_signals(
        self, *, domain: str, since: datetime
    ) -> list[Signal]: ...
```

Runs on a schedule against the Account table, not the lead inbox.

### 5.6 `DeepFetchStrategy` (already designed)

Covered in §3.1 of enrichment-pipeline-design.md. Per-source strategy
keyed on `lead.source`. Browser-backed strategies gate on
`LEADS_ENABLE_BROWSER_FETCHER`.

### 5.7 Module layout (additive to existing)

```
src/modules/enrichment/providers/
├── __init__.py
├── base.py                          # all 5 Protocols + ProviderResult
├── company_http_probe.py            # extracted from enrich_company.py
├── company_clearbit.py
├── company_builtwith.py
├── company_linkedin.py              # uses existing RapidAPI key
├── contact_linkedin.py              # full profile via RapidAPI
├── contact_hunter.py
├── contact_apollo.py
├── email_hunter.py
├── email_zerobounce.py
└── tech_builtwith.py
```

All built in [`Container.__init__`](../src/container.py#L29-L138), each
behind its own `LEADS_PROVIDER_*_KEY` config. A zero-provider chain is
legal — graceful degradation is a feature.

---

## 6. Data quality design

### 6.1 Canonical normalization (pure functions)

New module `src/modules/entities/normalize.py`:

| Field | Rule |
|---|---|
| `domain` | lowercase, strip scheme, strip leading `www.`, strip trailing `/`. Reject IPs. Normalize punycode. |
| `company_name` | trim, collapse whitespace, strip corporate suffixes (`Inc.`, `Ltd.`, `LLC`, `GmbH`, `, Inc`) for dedup — keep original as alias. |
| `email` | lowercase, trim. Reject invalid syntax before any API call. |
| `person_name` | title-case fallback, trim, collapse whitespace. |
| `title` | normalize to lowercase lookup, map via a static `title_ladder.yaml` → canonical title + seniority. |
| `country` | free text → ISO-3166-alpha-2 via a small lookup (`United States` / `USA` / `US` → `US`). |
| `industry` | LinkedIn codes + Clearbit categories → NAICS-2 (or our own stable taxonomy). |

All pure, all unit-testable, all callable from the Resolve stages.

### 6.2 Dedup keys

Generalise the current [`dedup.py`](../src/modules/scraping/dedup.py)
strategy into per-entity keys:

- **Account**: `canonical_domain` when present; else
  `sha256(canonical_name + country)`.
- **Contact**: `linkedin_url` (normalized) when present; else
  `sha256(lower(email))` when email present; else
  `sha256(lower(full_name) + canonical_domain)`.
- **Signal**: `sha256(account_id + signal_type + day_bucket + source_id)`
  (day_bucket from `dedup.py:37-41`).

Existing `raw_leads.dedup_hash` stays as the *inbox* dedup key — unchanged.

### 6.3 Field-level confidence

Every enrichment write to `Account` / `Contact` goes through a writer that:

1. Looks up the latest `ProviderResult` for that field.
2. Compares confidence × provider `trust_score` × freshness penalty.
3. Writes the winning value to `Account.field` and `Account.field_source`.
4. Appends the `ProviderResult` to the enrichment table regardless (audit).

Conflict resolution rule (simplest viable):
```
effective_confidence = raw_confidence * trust_score/100 * freshness_factor
                       freshness_factor = max(0, 1 - age_days / 180)
winner = max(effective_confidence)
```
Ties broken by provider precedence list in config
(`LEADS_PROVIDER_PRECEDENCE=["clearbit","linkedin","builtwith","http_probe"]`).

### 6.4 Freshness

Two layers:
- **Per-field TTL** via `AccountEnrichment.expires_at`. Default 90 days
  for firmographics; 30 days for employee counts; 60 days for contacts;
  7 days for signals.
- **Scheduled refresher** (new `RefreshWorker`) that wakes hourly, selects
  top-N accounts by recency-weighted score with expired fields, and
  re-runs the provider chain.

### 6.5 Email verification flow

1. Contact gets `email` set by `ContactDataProvider` → `email_status =
   'unverified'`.
2. Async `EmailVerificationWorker` batches unverified emails and calls
   `EmailVerificationProvider.verify(...)`.
3. Writes back `email_status`, `email_confidence`, `email_verified_at`.
4. Contacts with `email_status='invalid'` never surface in outreach lists.

### 6.6 Avoiding stale / excessive data

Covered in §9 of [enrichment-pipeline-design.md](./enrichment-pipeline-design.md):
- Body trim at ingestion (`enrichment_body_store_limit`).
- Body trim at prompt (`enrichment_body_char_limit`).
- PII redaction (`sanitize.py`).
- No raw HTML stored.
- No prompts or LLM responses persisted.
- Per-field expiry (§6.4 above).

---

## 7. Database migration plan

Existing migrations 001–004 untouched. **New migrations 005–008**, one
per phase. Each is backwards-compatible — existing code continues to read
`raw_leads.company_name` etc. until the entity model is switched on.

### 7.1 Migration 005 — enrichment hardening (already designed)

Covered in §6 of enrichment-pipeline-design.md.
- `raw_leads.enrichment_attempts`, `last_enrichment_error_at`
- `'insufficient'` status, `'max_attempts_reached'` status
- `lead_enrichments.deep_content_chars`, `insufficient_reason`
- New `enrichment_attempts`, `person_enrichments` tables
- `company_enrichments.provider`, `industry`, `country`

### 7.2 Migration 006 — accounts + contacts + data_sources

```sql
CREATE TABLE data_sources (
  id TEXT PRIMARY KEY,                          -- 'reddit', 'clearbit', ...
  kind TEXT NOT NULL,
  trust_score SMALLINT NOT NULL DEFAULT 50,
  requires_browser BOOLEAN NOT NULL DEFAULT false,
  notes TEXT
);

CREATE TABLE accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_domain TEXT UNIQUE,                 -- nullable (some leads have no domain)
  canonical_name TEXT NOT NULL,
  name_aliases TEXT[] NOT NULL DEFAULT '{}',
  industry TEXT,
  sub_industry TEXT,
  headcount_band TEXT,
  employee_count INT,
  employee_count_source TEXT REFERENCES data_sources(id),
  annual_revenue_min BIGINT,
  annual_revenue_max BIGINT,
  annual_revenue_currency TEXT,
  country TEXT,                                  -- ISO alpha-2
  region TEXT,
  city TEXT,
  funding_stage TEXT,
  last_funded_at DATE,
  total_funding_usd BIGINT,
  tech_stack TEXT[] NOT NULL DEFAULT '{}',
  linkedin_url TEXT,
  crunchbase_url TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_enriched_at TIMESTAMPTZ
);
CREATE INDEX ix_accounts_industry ON accounts (industry);
CREATE INDEX ix_accounts_country ON accounts (country);
CREATE INDEX ix_accounts_last_seen ON accounts (last_seen_at DESC);

CREATE TABLE contacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
  full_name TEXT NOT NULL,
  first_name TEXT,
  last_name TEXT,
  linkedin_url TEXT UNIQUE,
  email TEXT,
  email_status TEXT CHECK (email_status IN (
    'unverified','valid','invalid','catch_all','unknown')),
  email_confidence SMALLINT,
  email_verified_at TIMESTAMPTZ,
  title TEXT,
  title_normalized TEXT,
  seniority TEXT CHECK (seniority IN (
    'ic','senior_ic','manager','director','vp','c_level')),
  location TEXT,
  country TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_enriched_at TIMESTAMPTZ
);
CREATE INDEX ix_contacts_account ON contacts (account_id);
CREATE INDEX ix_contacts_email_status ON contacts (email_status);

ALTER TABLE raw_leads
  ADD COLUMN account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
  ADD COLUMN contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL;
CREATE INDEX ix_raw_leads_account ON raw_leads (account_id);
CREATE INDEX ix_raw_leads_contact ON raw_leads (contact_id);

-- Seed data_sources for every adapter currently registered
INSERT INTO data_sources (id, kind, trust_score) VALUES
  ('reddit','scraper',40), ('hackernews','scraper',50),
  ('hnhiring','scraper',65), ('wellfound','scraper',60),
  ('producthunt','scraper',55), ('rss','scraper',45),
  ('google_cse','scraper',50), ('linkedin','scraper',70),
  ('funding','scraper',70), ('remoteok','scraper',55);
```

### 7.3 Migration 007 — signals + field-level enrichment audit

```sql
CREATE TABLE signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,
  signal_type TEXT NOT NULL,
  signal_strength SMALLINT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source_id TEXT NOT NULL REFERENCES data_sources(id),
  source_url TEXT,
  raw_lead_id UUID REFERENCES raw_leads(id) ON DELETE SET NULL,
  dedup_key TEXT NOT NULL UNIQUE
);
CREATE INDEX ix_signals_account_time ON signals (account_id, observed_at DESC);
CREATE INDEX ix_signals_type_time ON signals (signal_type, observed_at DESC);

CREATE TABLE account_enrichment_fields (
  id BIGSERIAL PRIMARY KEY,
  account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  field_name TEXT NOT NULL,
  field_value JSONB NOT NULL,
  provider TEXT NOT NULL REFERENCES data_sources(id),
  confidence SMALLINT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_aef_account_field ON account_enrichment_fields (account_id, field_name);
CREATE INDEX ix_aef_expires ON account_enrichment_fields (expires_at);

CREATE TABLE contact_enrichment_fields (
  id BIGSERIAL PRIMARY KEY,
  contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  field_name TEXT NOT NULL,
  field_value JSONB NOT NULL,
  provider TEXT NOT NULL REFERENCES data_sources(id),
  confidence SMALLINT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_cef_contact_field ON contact_enrichment_fields (contact_id, field_name);

CREATE TABLE enrichment_runs (
  id BIGSERIAL PRIMARY KEY,
  lead_id UUID REFERENCES raw_leads(id) ON DELETE CASCADE,
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  provider TEXT REFERENCES data_sources(id),
  status TEXT NOT NULL CHECK (status IN
    ('ok','empty','error','rate_limited','timeout','skipped')),
  error_class TEXT,
  error_message TEXT,
  duration_ms INT,
  input_tokens INT, output_tokens INT, cost_usd NUMERIC(10,6),
  run_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_eruns_lead_time ON enrichment_runs (lead_id, run_at DESC);
CREATE INDEX ix_eruns_provider_time ON enrichment_runs (provider, run_at DESC);
```

### 7.4 Migration 008 — icp_profiles

```sql
CREATE TABLE icp_profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  industries TEXT[] NOT NULL DEFAULT '{}',
  headcount_bands TEXT[] NOT NULL DEFAULT '{}',
  countries TEXT[] NOT NULL DEFAULT '{}',
  annual_revenue_min BIGINT,
  annual_revenue_max BIGINT,
  required_stack TEXT[] NOT NULL DEFAULT '{}',
  avoided_stack TEXT[] NOT NULL DEFAULT '{}',
  signal_types TEXT[] NOT NULL DEFAULT '{}',
  min_signal_strength SMALLINT NOT NULL DEFAULT 30,
  target_titles TEXT[] NOT NULL DEFAULT '{}',
  target_seniorities TEXT[] NOT NULL DEFAULT '{}',
  keyword_seeds TEXT[] NOT NULL DEFAULT '{}',
  excluded_keywords TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 7.5 Retention

Added to `HousekeepingWorker`:
- `enrichment_runs` > 30 d → delete
- `llm_call_log` > 90 d → delete
- `account_enrichment_fields` WHERE `expires_at < now() - 180d` → delete
- `signals` > 365 d → archive (or delete — tenant call)

---

## 8. Configuration plan

Extends §7 of enrichment-pipeline-design.md. New settings grouped:

```python
# --- Enrichment providers (company) ---
enrichment_provider_clearbit_key: str = ""
enrichment_provider_builtwith_key: str = ""
enrichment_provider_linkedin_enabled: bool = True   # reuses LINKEDIN_RAPIDAPI_KEY

# --- Enrichment providers (contact) ---
enrichment_provider_hunter_key: str = ""
enrichment_provider_apollo_key: str = ""

# --- Email verification ---
enrichment_email_verify_provider: str = "hunter"    # 'hunter' | 'zerobounce' | 'neverbounce'
enrichment_email_verify_key: str = ""
enrichment_email_verify_batch_size: int = 50

# --- Provider trust & precedence ---
enrichment_provider_precedence: list[str] = [
    "clearbit","linkedin","builtwith","hunter","http_probe"
]

# --- Per-provider RPS ---
enrichment_provider_rps: dict[str, float] = {
    "clearbit": 2.0, "linkedin": 2.0, "hunter": 5.0, "apollo": 3.0,
}

# --- Per-provider daily USD budgets ---
enrichment_provider_daily_budget_usd: dict[str, float] = {
    "clearbit": 5.0, "hunter": 2.0, "apollo": 3.0,
}

# --- Field freshness (days) ---
freshness_employee_count_days: int = 30
freshness_firmographic_days: int = 90
freshness_contact_days: int = 60
freshness_signal_days: int = 7

# --- Confidence thresholds ---
min_confidence_promote_field: int = 40          # below this, field stays in audit only
min_confidence_for_outreach: int = 70           # below this, contact hidden from outreach

# --- ICP & query planning ---
icp_active_id: str = ""                         # "" = use env-driven queries (back-compat)
icp_query_cache_ttl_sec: int = 3600

# --- Refresh worker ---
refresh_interval_sec: int = 3600
refresh_batch_size: int = 100
```

Existing flags preserved:
- `LEADS_ENABLE_ENRICHMENT` — master gate, default `false`.
- `LEADS_ENABLE_BROWSER_FETCHER` — gated per enrichment design.
- `LEADS_USER_SKILLS` — becomes the fallback stack-match list when no
  ICP is active.

---

## 9. Scoring redesign

Today: [`compute_final_score()`](../src/modules/enrichment/scoring.py#L15-L56)
is one function taking one `EnrichmentResult`. Single-layer.

Proposed: three composable scores, combined into a final lead score.

### 9.1 Score layers

```
account_score   = f(icp_match, firmographic_confidence, tech_stack_overlap, funding_stage)
contact_score   = f(seniority, title_match, email_verified, email_confidence)
signal_score    = f(signal_strength, recency, signal_trust × source_trust)

final_score = w_a·account_score + w_c·contact_score + w_s·signal_score
              × data_confidence_penalty
              × sufficiency_floor
```

Where:
- `data_confidence_penalty ∈ [0.5, 1.0]` — drops as the share of
  low-confidence fields rises. Punishes leads with thin data; preserves
  strong ones.
- `sufficiency_floor` — hard-cap at 15 when the Sufficiency gate trips
  (mirrors current skip-penalty at
  [`scoring.py:53-54`](../src/modules/enrichment/scoring.py#L53-L54)).

Default layer weights: `w_a=0.45, w_c=0.25, w_s=0.30`. Configurable per
ICP.

### 9.2 Component breakdowns

**`account_score` (0–100):**
- ICP industry match: 30
- Headcount band in ICP: 20
- Country in ICP: 15
- Tech-stack overlap (sqrt-scaled, current [`_stack_match_score`](../src/modules/enrichment/scoring.py#L59-L77) logic): 20
- Funding recency (if ICP cares): 15

**`contact_score` (0–100):**
- Seniority in ICP target list: 40
- Title match (normalized): 30
- Email verified: 20
- Email confidence × 0.10: 10

**`signal_score` (0–100):**
- LLM `refined_signal_strength`: 40
- Recency (current 72h linear decay, see
  [`_recency_score`](../src/modules/enrichment/scoring.py#L80-L96)): 25
- Source trust (data_sources.trust_score / 100 × 20): 20
- Urgency from LLM: 15

### 9.3 Why per-ICP weights

Same account can score 85 for one ICP and 20 for another. The current
single scorer can't express that. Weights live in `icp_profiles` (new
table in migration 008) so changing targeting doesn't require deploys.

### 9.4 Re-scoring

Whenever `Account` / `Contact` / `Signal` rows change, a background
re-scorer recomputes `raw_leads.score` for affected leads. Piggy-backs on
the resweeper pattern — no new infrastructure needed.

---

## 10. Implementation roadmap

Five phases. Each phase shippable on its own. Bold = minimum to call v2
"production-ready."

### Phase 1 — Domain cleanup & dedupe foundation

**Goal**: introduce normalized entity layer without breaking current flow.

- **Migration 005** (enrichment hardening — already designed).
- **Migration 006** (accounts + contacts + data_sources).
- `src/modules/entities/` new package:
  - `normalize.py` — pure normalization functions (§6.1).
  - `resolver.py` — `AccountResolver`, `ContactResolver` classes.
- New Protocols in `domain/interfaces.py`:
  `AccountRepository`, `ContactRepository`.
- `PostgresLeadRepository` gains account/contact methods.
- Backfill script: for every existing `raw_leads` row, create/attach
  `account_id` and `contact_id` from the existing string fields (best-
  effort). Idempotent.
- **ResolveAccountStage** and **ResolveContactStage** replace current
  `ResolveCompanyStage`.
- `SignalStage` + `RecordSignal` — **deferred to Phase 2** if too large.

Files added/changed:
- [`alembic/versions/005_*.py`](../alembic/versions/) (new)
- [`alembic/versions/006_*.py`](../alembic/versions/) (new)
- `src/modules/entities/__init__.py`, `normalize.py`, `resolver.py`
- [`src/modules/enrichment/stages/resolve_company.py`](../src/modules/enrichment/stages/resolve_company.py) → split into `resolve_account.py`, `resolve_contact.py`
- [`src/modules/enrichment/pipeline.py`](../src/modules/enrichment/pipeline.py) — stage list updated
- [`src/infrastructure/postgres_repo.py`](../src/infrastructure/postgres_repo.py) — account/contact Tables + methods

### Phase 2 — Firmographic enrichment (company provider chain)

**Goal**: replace the HEAD-probe with a real provider chain.

- **Migration 007** (signals + enrichment_fields + enrichment_runs).
- `src/modules/enrichment/providers/` new package (see §5.7).
- Extract [`EnrichCompanyStage`](../src/modules/enrichment/stages/enrich_company.py)
  into `company_http_probe.py`.
- Add `company_clearbit.py`, `company_builtwith.py`,
  `company_linkedin.py`.
- `EnrichAccountStage` replaces `EnrichCompanyStage`, iterates the chain,
  writes `account_enrichment_fields` rows, updates `Account` via the
  confidence-winner rule (§6.3).
- `RecordSignalStage` writes `signals` row.
- Rate-limit + budget per provider in `infrastructure/rate_limit.py`
  (already proposed in enrichment design).

### Phase 3 — Contact enrichment & email verification

**Goal**: first-class contacts with verified emails.

- `contact_linkedin.py` (uses existing RapidAPI),
  `contact_hunter.py`, `contact_apollo.py`.
- `EnrichContactStage` — provider chain, writes
  `contact_enrichment_fields`.
- `email_hunter.py`, `email_zerobounce.py`.
- `EmailVerificationWorker` — batched, opt-in.
- Pipeline resequenced per §4.2.

### Phase 4 — Signal + context enrichment

**Goal**: time-to-reach-out becomes a first-class signal, plus outreach-
ready context.

- `DeepFetchStrategy` family
  (see [enrichment-pipeline-design.md §3.1](./enrichment-pipeline-design.md)).
- Browser-backed strategies enabled in prod (see §4 of enrichment doc).
- Signal-specific enrichment: funding-round dedup, hiring-page resolution.
- `OutreachContext` materialization — SMART-model output per contact,
  cached, regeneratable.
- **Migration 008** (icp_profiles).
- `modules/scraping/query_planner.py` — ICP → adapter ScrapeRequests.
- Route: `POST /icp/{id}/scrape`.

### Phase 5 — Scoring & observability

**Goal**: production-grade scoring and ops visibility.

- Three-layer scorer (§9). Reuses the current pure-function style.
- Background re-scorer on entity change.
- `GET /health/enrichment` returns per-provider counters, per-ICP lead
  counts, daily budget burn.
- Dashboards (Grafana or equivalent): provider success rate, cost per
  ICP, signals per day, account growth.
- Audit export: per-lead ancestry from `enrichment_runs`.

---

## 11. Testing strategy

### 11.1 Unit (fast, no IO)

- **Normalization**: one test per field in `normalize.py`; property-based
  (`hypothesis`) for domain/email/name roundtrips.
- **Dedup**: `AccountResolver.dedup_key()` stability across input
  variations; `ContactResolver` likewise.
- **Provider chain**: chain merges first non-empty per field; disabled
  provider skipped; all-fail returns empty `ProviderResult`.
- **Confidence resolution**: winner-takes-all math, tie-break by
  precedence list, freshness decay.
- **Scoring**: each layer independently; per-ICP weight overrides;
  sufficiency floor.
- **Rate limiter**: token bucket burst + refill.
- **Retry**: retries transient errors; skips permanent.

### 11.2 Integration (testcontainers Postgres)

- **End-to-end happy path** per source — raw_lead → scored with full
  entity graph.
- **Dedup end-to-end** — same company in 3 different raw_leads → one
  account, three signals.
- **Provider fallback** — Clearbit 500 → BuiltWith succeeds → account
  filled, `account_enrichment_fields` rows show both attempts.
- **Email verification** — batched worker, async write-back.
- **Idempotency** — replay of the same `LeadCreated` event is safe.
- **Budget exceeded** — per-provider budget gate → `status='budget_paused'`.
- **Migration round-trips** — upgrade + downgrade each migration cleanly.

### 11.3 Contract tests (regression guards)

- `LEADS_ENABLE_ENRICHMENT=false` → scraping still writes `raw_leads`,
  no worker tasks, no provider calls. Mirrors the existing
  disabled-parity guard.
- `LEADS_ENABLE_BROWSER_FETCHER=false` → browser-dependent sources
  gracefully degrade; non-browser sources unaffected.
- Removing all provider keys → pipeline still completes; scores lower but
  no crashes.

### 11.4 Load (manual, pre-prod)

- Seed 5 000 synthetic leads spanning all sources.
- Mock LLM + mock providers with realistic latency distributions.
- Assert: concurrency respects `max_concurrent_enrichments`; queue drops
  recovered by resweeper; browser pool memory flat after 10 000 pages;
  p95 per-lead enrichment latency < 30 s.

### 11.5 Data-quality regression tests

- Golden dataset: 100 labelled leads with expected
  `{account, contact, signal, score}`. Run weekly in CI. Fail PR if any
  score drifts > 10 points without an explicit weight-change entry in
  the changelog.

---

## 12. Risks and tradeoffs

### Technical

- **Schema expansion**: going from ~8 tables to ~14 increases surface
  area. Mitigation: migrations are strictly additive; old tables
  (`raw_leads`, `company_enrichments`) stay until Phase 5 retirement (if
  ever).
- **Resolver correctness**: bad Account dedup can merge two real
  companies. The normalized-domain primary key is conservative, but name-
  only resolution (no domain) will have false positives. Mitigation:
  don't merge on name-only; create separate accounts and let the resolver
  flag suspected duplicates for manual review.
- **Three-layer scoring complexity**: harder to explain than the current
  one-liner. Mitigation: the scorer stays a pure function; a debug route
  returns the per-layer breakdown per lead.
- **Provider chains amplify latency**: p95 per lead could grow from 5 s
  to 30 s. Mitigation: chains run with per-provider timeouts; the pipeline
  fans out on accounts in parallel up to `max_concurrent_enrichments`.

### Data quality

- **Silent drift** of field values when providers update stale data.
  Mitigation: freshness decay in confidence math; scheduled refresher.
- **Title taxonomy rot**: `title_ladder.yaml` ages. Mitigation: version
  the YAML, log unmatched titles, review monthly.
- **LLM hallucination in classify** when DeepFetch returns empty.
  Mitigation: Sufficiency gate; prompts instructed to return `skip` when
  context is thin.

### Compliance

- **PII on contacts**: emails, LinkedIn URLs, location. Mitigation:
  - PII redaction in prompts (`sanitize.py`).
  - TTL on `contacts.email` (revisit at 60 days).
  - GDPR-compatible `DELETE /contacts/{id}` that cascades to
    enrichment_fields + enrichment_runs.
  - Never store raw profile HTML.
- **Provider ToS**: LinkedIn scraping via RapidAPI is in a grey area.
  Decision: rely on the RapidAPI host's own ToS relationship; don't do
  direct LinkedIn scraping. Document this in
  [README](../README.md).

### Cost

- **Provider API spend**: Clearbit ~$99/mo baseline, Hunter per-lookup,
  Apollo subscription. Mitigation: per-provider daily budget in config
  (§8); LLM budget ceiling already in place.
- **Browser fetcher infra**: Chromium RAM + CPU. Mitigation: single
  pooled instance with page recycling already exists
  ([`BrowserPool`](../src/infrastructure/fetchers/browser.py#L35-L145)).

### Operational

- **Backfill risk**: Phase 1 backfill touches every `raw_leads` row. Must
  be chunked + idempotent. Mitigation: background script with
  configurable batch size; resume-safe.
- **Schema migration windows**: adding FKs on large tables locks writes.
  Mitigation: Alembic scripts use `NOT VALID` + `VALIDATE` pattern for
  constraints; FK columns added NULL first.
- **Observability debt**: more moving parts = more to monitor. Mitigation:
  every new stage emits structlog with consistent keys
  (`lead_id`, `account_id`, `provider`, `stage`); `/health/enrichment`
  extended per phase.

### Accepted tradeoffs

- **No true graph DB**: Postgres with FKs is enough at this scale;
  Neo4j-style queries aren't needed until cross-account relationships
  (org charts, parent-companies) become first-class.
- **Not multi-tenant**: one ICP-per-install for now. Making this
  multi-tenant is a ~Phase 6 lift and out of scope here.
- **No learned scoring**: handcrafted weights are auditable and tunable
  without labelled conversion data we don't yet have. Revisit once
  outreach telemetry gives us a training signal.

---

## Appendix — files referenced (repo paths)

| Concern | File |
|---|---|
| Settings | [`src/config.py`](../src/config.py) |
| DI wiring | [`src/container.py`](../src/container.py) |
| Domain models | [`src/domain/models.py`](../src/domain/models.py) |
| Protocols | [`src/domain/interfaces.py`](../src/domain/interfaces.py) |
| Event bus | [`src/application/bus.py`](../src/application/bus.py) |
| Workers | [`src/application/workers.py`](../src/application/workers.py) |
| Orchestrator | [`src/modules/scraping/orchestrator.py`](../src/modules/scraping/orchestrator.py) |
| Signal classifier | [`src/modules/scraping/signals.py`](../src/modules/scraping/signals.py) |
| Dedup | [`src/modules/scraping/dedup.py`](../src/modules/scraping/dedup.py) |
| Adapters | [`src/modules/scraping/adapters/`](../src/modules/scraping/adapters/) |
| Enrichment pipeline | [`src/modules/enrichment/pipeline.py`](../src/modules/enrichment/pipeline.py) |
| Stages | [`src/modules/enrichment/stages/`](../src/modules/enrichment/stages/) |
| Scoring | [`src/modules/enrichment/scoring.py`](../src/modules/enrichment/scoring.py) |
| LinkedIn enricher (already built) | [`src/modules/enrichment/linkedin_enricher.py`](../src/modules/enrichment/linkedin_enricher.py) |
| Repository | [`src/infrastructure/postgres_repo.py`](../src/infrastructure/postgres_repo.py) |
| HTTP fetcher | [`src/infrastructure/fetchers/http.py`](../src/infrastructure/fetchers/http.py) |
| Browser fetcher | [`src/infrastructure/fetchers/browser.py`](../src/infrastructure/fetchers/browser.py) |
| API routes | [`src/api/routes.py`](../src/api/routes.py) |
| Migrations | [`alembic/versions/`](../alembic/versions/) |
| Enrichment stage-level design | [`docs/enrichment-pipeline-design.md`](./enrichment-pipeline-design.md) |
