# Lead Pipeline — Modular Monolith

A production-grade service that scrapes leads from external sources, enriches them with LLM-powered classification, scores them, and persists everything to PostgreSQL. Two modules (scraping + enrichment) run in one process, communicating via an in-process event bus.

## Architecture Overview

This is a **modular monolith**, not a microservices architecture. The original design split scraping and enrichment into separate services communicating via Redis Streams. At single-developer, single-node scale that was over-engineered. We collapsed to one service with two modules, sharing one process, one Postgres database, one deploy.

Module boundaries are enforced by code structure and protocols, not network hops.

```
┌─────────────────────────────────────────────────────────────┐
│                      FastAPI Process                         │
│                                                              │
│  ┌──────────────┐    EventBus     ┌───────────────────────┐ │
│  │   Scraping    │───LeadCreated──▶│     Enrichment        │ │
│  │   Module      │                 │     Module            │ │
│  │              │                 │  6-stage pipeline:    │ │
│  │  Reddit      │                 │  fetch → resolve →    │ │
│  │  HackerNews  │                 │  enrich → classify →  │ │
│  │  (adapters)  │                 │  score → persist      │ │
│  └──────┬───────┘                 └──────────┬────────────┘ │
│         │                                     │              │
│         └──────────┐              ┌───────────┘              │
│                    ▼              ▼                           │
│              ┌─────────────────────────┐                     │
│              │       PostgreSQL         │                     │
│              │  raw_leads              │                     │
│              │  lead_enrichments       │                     │
│              │  company_enrichments    │                     │
│              │  llm_call_log          │                     │
│              └─────────────────────────┘                     │
└─────────────────────────────────────────────────────────────┘
```

### Postgres-as-Durable-Queue Pattern

**This is the single most important architectural decision.** It's what makes the in-process event bus safe without Redis Streams:

1. The scraping orchestrator inserts a row with `status='new'` **before** publishing a `LeadCreated` event to the bus.
2. The enrichment pipeline's persist stage updates status to `scored` (or `enrichment_failed` or `budget_paused`).
3. The **PendingLeadsResweeper** runs every 5 minutes, finds leads stuck in intermediate statuses (`new`, `pending_enrichment`, `budget_paused`) for >10 minutes, and re-publishes `LeadCreated` events.

This means the Postgres `status` column is the durable queue. The event bus is just the fast path. If the process crashes, the queue overflows, or a handler throws — the resweeper recovers everything on restart.

**The event bus is an optimization, not a reliability mechanism.** Postgres is the source of truth.

## Layers

| Layer | Path | Responsibility |
|-------|------|---------------|
| Domain | `src/domain/` | Models, protocols, events |
| Application | `src/application/` | EventBus, background workers |
| Modules | `src/modules/scraping/` | Adapters, orchestrator, dedup |
| Modules | `src/modules/enrichment/` | 6-stage pipeline, scoring, resolver |
| Infrastructure | `src/infrastructure/` | Postgres, Redis, HTTP, LLM providers |
| API | `src/api/` | FastAPI routes, DI providers |

## Quick Start

### With Docker Compose

```bash
cp .env.example .env
# Edit .env to add your LEADS_ANTHROPIC_API_KEY
docker compose up --build
```

### Local Development

```bash
pip install -e ".[dev]"
docker compose up postgres redis -d
alembic upgrade head
uvicorn main:app --reload --app-dir src
```

### Trigger a Scrape + Enrichment

```bash
# Scrape Reddit — inserts leads and publishes LeadCreated events
curl -X POST http://localhost:8000/api/v1/scrape/reddit | jq

# Manually enrich a specific lead
curl -X POST http://localhost:8000/api/v1/enrich/{lead_id} | jq

# Check LLM costs
curl http://localhost:8000/api/v1/stats/cost | jq
```

## Source Adapters

| Source | Fetcher | Poll Interval | Signal | Requires |
|--------|---------|--------------|--------|----------|
| Reddit | HttpFetcher | 10 min | All signal types via regex | — |
| Hacker News | HttpFetcher | 15 min | All signal types via regex | — |
| RemoteOK | RssFetcher | 10 min | HIRING (job board) | — |
| Wellfound | BrowserFetcher | 1 hr | HIRING (startup jobs) | `ENABLE_BROWSER_FETCHER=true` |
| ProductHunt | RssFetcher | 6 hr | GENERAL_INTEREST / TOOL_EVALUATION | — |
| RSS Feeds | RssFetcher | 30 min | All signal types via regex | `RSS_FEED_URLS` configured |
| Google CSE | HttpFetcher | 6 hr | All signal types via regex | `GOOGLE_CSE_API_KEY` |
| LinkedIn | BrowserFetcher | 4 hr | HIRING (job listings) | `ENABLE_BROWSER_FETCHER=true` |
| Funding | RssFetcher | 12 hr | FUNDING (round stage scoring) | — |

Adapters are conditionally registered — browser-based ones only appear when Playwright is enabled, API-key-dependent ones only when keys are set, RSS multi-feed only when URLs are configured.

## How the Enrichment Pipeline Works

The pipeline runs 6 stages sequentially for each lead:

| # | Stage | What it does | I/O |
|---|-------|-------------|-----|
| 1 | **Fetch** | Load lead from DB, check idempotency (skip if already scored) | DB read |
| 2 | **Resolve Company** | If company_domain missing, use cheap LLM to extract it. Cached. | LLM (cheap) |
| 3 | **Enrich Company** | HTTP HEAD + title scrape on domain. Cached 30 days. | HTTP |
| 4 | **Classify** | SMART LLM call for structured classification (signal type, scores, approach) | LLM (smart) |
| 5 | **Score** | Pure function combining LLM scores + recency + stack match. No I/O. | None |
| 6 | **Persist** | Write enrichment to DB, update lead status to 'scored', publish LeadScored. | DB write |

Each stage receives a `PipelineContext` and returns a new (immutable) context with additional fields.

## How to Add a New Pipeline Stage

1. Create `src/modules/enrichment/stages/your_stage.py` with:
   ```python
   class YourStage:
       def __init__(self, ...dependencies...) -> None: ...
       async def execute(self, context: PipelineContext) -> PipelineContext: ...
   ```
2. Add it to the stage list in `src/modules/enrichment/pipeline.py`
3. If it needs new data, extend `PipelineContext` in `domain/models.py`

## Fetch Layer

Adapters don't call httpx directly. Instead, three shared fetchers handle all HTTP concerns (retry, backoff, rate-limit parsing, timeouts) so adapters contain only source-specific logic.

| Mode | Fetcher | Use when | Example adapter |
|------|---------|----------|----------------|
| **HTTP/JSON** | `HttpFetcher` | Source has a REST/JSON API | Reddit, HackerNews |
| **RSS/Atom** | `RssFetcher` | Source publishes an RSS feed | RemoteOK |
| **Browser** | `BrowserFetcher` | Source requires JS rendering | (future, optional) |

`HttpFetcher` provides retry with exponential backoff (1s → 4s → 16s), 429/Retry-After handling, rate-limit header parsing, and typed exceptions. `RssFetcher` delegates HTTP to `HttpFetcher` and adds feedparser. `BrowserFetcher` uses Playwright (optional dependency, `pip install "lead-pipeline[browser]"`).

## How to Add a New Source Adapter

**HTTP/JSON adapter** (e.g. a new API source):
```python
# src/modules/scraping/adapters/your_source.py
class YourAdapter:
    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None: ...
    async def fetch_raw(self) -> list[dict]:
        resp = await self._fetcher.get_json("https://api.source.com/data")
        return resp.data["items"]
    def normalize(self, raw: dict) -> CanonicalLead | None: ...
```

**RSS adapter** (e.g. a new job board):
```python
class YourRssAdapter:
    def __init__(self, fetcher: RssFetcher, settings: Settings) -> None: ...
    async def fetch_raw(self) -> list[dict]:
        feed = await self._fetcher.fetch("https://source.com/feed.rss")
        return [entry_to_dict(e) for e in feed.entries]
    def normalize(self, raw: dict) -> CanonicalLead | None: ...
```

**Browser adapter** (e.g. a JS-rendered page):
```python
class YourBrowserAdapter:
    def __init__(self, fetcher: BrowserFetcher, settings: Settings) -> None: ...
    async def fetch_raw(self) -> list[dict]:
        html = await self._fetcher.fetch_html("https://source.com", wait_for_selector=".listing")
        return parse_html(html)
```

Then register in `src/modules/scraping/adapters/__init__.py` and add config if needed.

## How to Swap LLM Providers

1. Implement the `LLMProvider` protocol in `src/infrastructure/your_provider.py`
2. Update `Container.__init__` in `src/container.py` to instantiate your provider
3. Map `ModelHint.CHEAP` and `ModelHint.SMART` to your provider's model IDs via config

The `OpenAIProvider` stub in `src/infrastructure/openai_provider.py` shows the shape.

## Cost Monitoring

- `GET /api/v1/stats/cost` — aggregated LLM costs by day, stage, model
- `GET /api/v1/health/enrichment` — today's spend vs budget, worker health
- `LEADS_DAILY_LLM_BUDGET_USD` — hard ceiling. When exceeded, leads get `budget_paused` status and are retried next day by the resweeper

## Environment Variables

See `.env.example` for the full list. All prefixed with `LEADS_`.

| Variable | Default | Description |
|----------|---------|-------------|
| `LEADS_DATABASE_URL` | `postgresql+asyncpg://...` | Postgres connection |
| `LEADS_REDIS_URL` | `redis://localhost:6379/0` | Redis (caching only) |
| `LEADS_ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `LEADS_LLM_MODEL_CHEAP` | `claude-haiku-4-5-20251001` | Model for cheap calls |
| `LEADS_LLM_MODEL_SMART` | `claude-sonnet-4-6` | Model for smart calls |
| `LEADS_DAILY_LLM_BUDGET_USD` | `10.0` | Daily spending ceiling |
| `LEADS_MAX_CONCURRENT_ENRICHMENTS` | `5` | Pipeline parallelism |
| `LEADS_EVENT_BUS_QUEUE_SIZE` | `1000` | Max buffered events |
| `LEADS_RESWEEPER_INTERVAL_SECONDS` | `300` | Resweep frequency |

## Running Tests

```bash
# Unit tests only (no containers needed)
pytest tests/test_dedup.py tests/test_reddit_adapter.py tests/test_orchestrator.py \
       tests/test_event_bus.py tests/test_scoring.py tests/test_enrichment_stages.py -v

# Integration tests (requires Docker for testcontainers)
pytest tests/test_integration.py tests/test_enrichment_integration.py tests/test_resweeper.py -v

# All tests
pytest -v

# Type checking
mypy

# Linting
ruff check src/ tests/
```

## Migration Path to Microservices (if ever needed)

The modular monolith is designed so splitting into services later is straightforward:

1. The `EventBus` publish/consume interface maps directly to a message broker (Redis Streams, Kafka, NATS)
2. Each module depends only on protocols — swap the in-process implementation for an HTTP/gRPC client
3. The `PendingLeadsResweeper` pattern works identically with an external queue
4. Database tables are already cleanly separated by module concern

But: don't split until you need to. The single-process architecture eliminates network latency, distributed transactions, and deployment complexity.

## Future Work

- Prometheus metrics export (`/metrics` endpoint)
- OpenTelemetry tracing for distributed observability
- Scheduled polling via APScheduler or a cron trigger
- Outreach module (next module in the pipeline)
- Admin dashboard for monitoring
- Multi-tenancy support
