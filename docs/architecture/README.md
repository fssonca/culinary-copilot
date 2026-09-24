# Current system architecture

Code snapshot: **2026-09-23**, including the uncommitted clarification backend.
This describes implemented behavior, not the eventual agent design. Start here;
then read [request flows](request-flows.md) and [data and ingestion](data-and-ingestion.md).
Diagrams use Mermaid, which GitHub renders directly.

## 1. The system in plain language

The backend has three main jobs:

1. **Prepare recipe data:** explicit ingestion commands normalize dataset rows,
   optionally ask a model to interpret ambiguous text, validate results, and load PostgreSQL.
2. **Find stored recipes:** API endpoints search both datasets or fetch one canonical recipe.
3. **Clarify a cooking request:** rules and an optional model propose questions;
   typed answers update server-owned, temporary conversation state.

There is no frontend, finished recommendation workflow, recipe generation,
vector retrieval, or autonomous agent loop yet. Clarification readiness is a
signal for a future retrieval integration, not a generated meal recommendation.

```mermaid
flowchart TB
    Client["Client: curl, Swagger, or future UI"] --> API["FastAPI backend"]
    API --> Recipes["Recipe search and lookup"]
    Recipes --> PG[("PostgreSQL: shared recipe corpus")]
    API --> Clarify["Clarification services"]
    Clarify --> Rules["Deterministic question and answer rules"]
    Clarify --> Memory[("Process-local conversation store")]
    Clarify -->|"optional title and ID evidence"| Recipes
    Clarify -->|"LLM_ENABLED"| AppLLM["Interactive application provider"]
    AppLLM --> OpenAI["OpenAI service"]
    API --> Pairings["Epicure pairing endpoint"]
    Pairings --> Epicure["Local Epicure vectors"]
    CLI["Operator-run ingestion commands"] --> Parse["Parse, route, validate, merge"]
    Parse -->|"LLM_INGESTION_ENABLED; explicit submit"| Batch["OpenAI Batch extraction"]
    Batch --> OpenAI
    Batch --> Parse
    Parse -->|"explicit load"| PG
    CLI --> Artifacts[("Local data artifacts and manifests")]
```

Arrows show calls/data flow, not a single execution sequence. Ingestion does
not run during API startup. The two LLM paths have different prompts, schemas,
settings and lifecycles. Epicure is a local similarity model, not the LLM.

## 2. Where code belongs

All paths below are relative to `src/culinary_copilot/`.

| Area | Responsibility | Main files |
|---|---|---|
| API | HTTP validation, endpoint dispatch, lifecycle | `api/app.py`, `api/clarification.py`, `api/retrieval.py` |
| Retrieval | Ready-request mapping, bounded evidence summaries | `retrieval/query.py`, `retrieval/service.py` |
| Domain | Cooking request, questions, answers, status and rules | `domain/requests.py`, `domain/clarification.py`, `domain/rule_planner.py` |
| Services | Planning orchestration, answer updates, concurrency | `services/clarification_service.py`, `hybrid_planner.py`, `answers.py`, `store.py` |
| Application LLM | Async provider interface, fake provider, bounded retries | `llm/client.py` |
| Recipe access | Parameterized SQL search and document lookup | `recipes/repository.py`, `search.py` |
| Ingestion | Source normalization, extraction, validation and loading | `recipes/import_data.py`, `adapters/`, `llm_batch.py`, `llm_sched.py`, supporting modules |
| Epicure | Load pinned vocabulary/vectors and calculate neighbors | `tools/epicure.py` |
| Infrastructure | Settings, database engine, planning events | `config.py`, `db.py`, `obs/clarification.py` |

Development-only dataset tools are under `scripts/datasets/`. Production code
does not import them. Tests live under `tests/`; `evals/` is an evaluation
scaffold, not yet a measured retrieval benchmark.

## 3. Deployment and lifetime

```mermaid
flowchart LR
    Host["Developer terminal"] -->|"localhost:8000"| API["API container or local uvicorn"]
    Host -->|"localhost:5432"| DB["PostgreSQL 17 container"]
    API -->|"db:5432 in Compose; localhost locally"| DB
    DB --> Disk[("backend_postgres_data volume")]
    API --> RAM[("Clarification state in process memory")]
    API --> Cache[("huggingface_cache volume in Compose")]
```

- Compose runs `api` and `db`; CLI ingestion is a separate operator action.
- PostgreSQL and cached model files survive container replacement through named volumes.
- Conversation state does **not** survive an API restart. Each worker would have
  its own state: the current store is for a single-process development deployment.
- The store caps retained requests at 512 and removes associated groups on eviction.
- App startup creates the engine, services and store. It initializes the application
  provider when enabled; shutdown closes that client and disposes the engine.
- `/health/live` reports process availability; `/health/ready` checks PostgreSQL
  connectivity. Neither certifies model availability, corpus quality, or conversation persistence.

## 4. Independent switches

| Switch | Enables | Does not enable |
|---|---|---|
| `LLM_ENABLED` | Optional interactive clarification planning | Batch ingestion or recipe generation |
| `LLM_INGESTION_ENABLED` | Explicit ingestion extraction calls | Interactive clarification |
| `EPICURE_ENABLED` | Local Epicure pairing capability | Dietary certification or automatic recommendation generation |

Defaults are disabled in code. A request can also set `use_llm=false` for rule-only
clarification. These are configuration defaults, not a claim about local `.env` values.
Application provider limits use `LLM_APP_*`; ingestion limits use separate settings.
One application planning operation can have bounded transport retries, so “one
planning call” does not necessarily mean exactly one network attempt.

## 5. Implemented boundaries and remaining gaps

| Capability | Current state |
|---|---|
| Dataset-aware search/lookup | Implemented across Food.com and Foodie |
| Hybrid clarification and typed answers | Implemented; model behavior tested with fakes |
| Explicit-answer correction protection | Model changes require confirmation; typed edits supported |
| Recipe context inside planning | Up to three title/ID records; not complete source documents |
| Epicure inside clarification | Helper exists, but caller supplies an empty ingredient; no effective pairing lookup |
| Clarification-to-result retrieval workflow | Implemented (Phase 1, repaired): current-group `POST /api/v1/retrieval/search` maps ready requests to eligibility/ranking search plus bounded exact-pair summaries; see `docs/retrieval.md` |
| Substitution verification | Limited checks; suggestions remain unverified, not certified equivalents |
| Durable conversations | Not implemented |
| Recipe embeddings / pgvector | Not implemented; existing `search_vector` is PostgreSQL full text |
| Generation, streaming, web search, agent iteration | Not implemented |

Important limits: conflict checks are keyword-based; source quote matching for
inferred fields is substring-based, not semantic proof. Planning event logging
exists, but early rule-only/error exits currently bypass the emission site; it
is not complete per-request telemetry. The emitted planning group ID is currently
`pending`, before a real group is created.

## 6. How to verify and navigate

`make check` runs Ruff, formatting, mypy and tests/evals. Default model tests use
fake providers. PostgreSQL integration tests use disposable databases when available.
No live model reliability claim follows from fake-provider tests.

- [Clarification contracts and HTTP examples](../clarification.md)
- [Food.com import runbook](../recipe-ingestion.md)
- [Hybrid ingestion and completed local migration](../hybrid-ingestion.md)
- [Database schema and ingestion diagrams](data-and-ingestion.md)
- [Request lifecycle and concurrency diagrams](request-flows.md)
