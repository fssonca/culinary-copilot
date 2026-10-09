# Current system architecture

Code snapshot: **2026-10-05 (Milestone 3, Phases 1-7 done; live
evaluation run and reviewed at checkpoint C; milestone acceptance
pending)**. The bounded agent loop, permission-gated web search and
minimal UI are implemented as described below. This page describes
implemented behavior, not the eventual agent design. Diagrams use
Mermaid, which GitHub renders directly.

**Reading order:**

1. **[The agent system](agent-system.md):** a one-sitting explanation
   of the agent. It covers the design principles, components, a run
   step by step, context management, memory, human approvals,
   guardrails, security, observability, evaluation and limits.
2. **[Agentic development workflow](agentic-development.md):** how the
   system is built with AI coding agents. It covers roles, guardrails,
   human checkpoints, verification and paid-run governance.
3. This page, which places the agent within the whole backend,
   including ingestion, clarification and retrieval.
4. [Request flows](request-flows.md) and
   [data and ingestion](data-and-ingestion.md).

## 1. The system in plain language

The backend has five main jobs:

1. **Prepare recipe data:** explicit ingestion commands normalize dataset rows,
   optionally ask a model to interpret ambiguous text, validate results, and load PostgreSQL.
2. **Find stored recipes:** API endpoints search both datasets or fetch one canonical recipe.
3. **Clarify a cooking request:** rules and an optional model propose questions;
   typed answers update server-owned, temporary conversation state.
4. **Recommend a stored recipe:** consult Epicure, fetch source evidence, ask a
   bounded model to select, then validate and render recipe facts and propositions
   on the server.
5. **Run the cooking agent (Milestone 3):** a bounded tool-using loop that
   works through a Postgres-backed session from request to cooking plan. It
   asks when an answer matters, waits for the user's dish choice, searches
   the web only with permission, and passes every answer through
   deterministic validators. See [the agent system](agent-system.md).

The agent loop is implemented (`agent/loop.py`, hand-written, no
framework): native function calling over the typed registry, parallel
calls in call order, CAS plus events per step, server-set budgets
(12 steps, 12 tool calls, 60k/12k tokens, 90 s wall clock) with stable
stop reasons, Epicure queried by default, validation as the last gate
(see `docs/agent.md` and `docs/adr/0002-agent-loop.md`). `search_web`
is implemented behind the same registry (server-bound session,
atomic slot claim max 3/session, one bounded hosted sub-request per
dispatch; see `docs/tools.md`). The minimal UI is served at `/ui`
(native ES modules, no build; toggle off by default, backend
enforces; see `docs/ui-walkthrough.md`). Vector and hybrid
retrieval exist in the retrieval service and the evaluation harness, but
the API and recommendations run full-text by default (code default
`fulltext`; see ADR 0001).
Clarification readiness feeds the implemented recommendation
workflow (source-grounded selection, backend-only), served over both
non-streaming JSON and versioned SSE streaming (Phase 4, shared
service). Phase 3's ordinary and
native-tool paths have bounded live evidence and owner acceptance based on
AI-assisted review. Structural admission does not certify completeness or
practical usefulness; see [closure and limitations](../phase3-closure.md).

```mermaid
flowchart TB
    Client["Client: curl, Swagger, or /ui page"] --> API["FastAPI backend"]
    API --> Agent["Bounded agent loop (sessions, typed tools, validators)"]
    Agent --> Sessions[("PostgreSQL: sessions + append-only events")]
    Agent --> Recipes
    Agent -->|"LLM_RECOMMENDATION_ENABLED"| AppLLM
    Agent -->|"pairing tools"| Epicure
    Agent -->|"permission on"| WebSearch["Hosted web search"]
    API --> Recipes["Recipe search and lookup"]
    Recipes --> PG[("PostgreSQL: shared recipe corpus")]
    API --> Clarify["Clarification services"]
    Clarify --> Rules["Deterministic question and answer rules"]
    Clarify --> Memory[("Process-local conversation store")]
    Clarify -->|"optional title and ID evidence"| Recipes
    Clarify -->|"LLM_ENABLED"| AppLLM["Interactive application provider"]
    AppLLM --> OpenAI["OpenAI service"]
    API --> Retrieval["Ready-request retrieval"]
    Retrieval --> Memory
    Retrieval --> Recipes
    Retrieval -.->|"vector/hybrid: service and eval harness only"| QueryEmbed["Query embedding"]
    QueryEmbed --> OpenAI
    API --> Recommend["Grounded recommendation service"]
    Recommend --> Memory
    Recommend --> Recipes
    Recommend -->|"cached consultation"| Epicure
    Recommend -->|"LLM_RECOMMENDATION_ENABLED"| AppLLM
    Recommend --> Validation["Validate selection and propositions"]
    Validation --> Render["Server-render source recipe and wording"]
    API --> Pairings["Epicure pairing endpoint"]
    Pairings --> Epicure["Local Epicure vectors"]
    CLI["Operator-run ingestion commands"] --> Parse["Parse, route, validate, merge"]
    Parse -->|"LLM_INGESTION_ENABLED; explicit submit"| Batch["OpenAI Batch extraction"]
    Batch --> OpenAI
    Batch --> Parse
    Parse -->|"explicit load"| PG
    CLI --> Artifacts[("Local data artifacts and manifests")]
    CLI -->|"explicit embedding run"| EmbedCLI["Recipe embedding backfill"]
    EmbedCLI --> OpenAI
    EmbedCLI --> PG
```

Arrows show calls/data flow, not a single execution sequence. The dotted
arrow is a path the HTTP API does not take today. Ingestion and embedding
backfills do not run during API startup. The two LLM paths have different prompts, schemas,
settings and lifecycles. Epicure is a local similarity model, not the LLM.

## 2. Where code belongs

All paths below are relative to `src/culinary_copilot/`.

| Area | Responsibility | Main files |
|---|---|---|
| API | HTTP validation, endpoint dispatch, lifecycle | `api/app.py`, `api/clarification.py`, `api/retrieval.py`, `api/recommendations.py` |
| Retrieval | Ready-request mapping, bounded evidence summaries, retrieval modes (full-text/vector/hybrid, cutoff, gate) | `retrieval/query.py`, `retrieval/service.py`, `retrieval/hybrid.py` |
| Embeddings | Embedding provider, query embedding, model registry, document rendering | `embeddings/provider.py`, `query.py`, `registry.py`, `rendering.py` |
| Recommendations | Grounded selection, Epicure consultation, tool mode | `recommendations/service.py`, `evidence.py`, `policy.py`, `prompts.py`, `propositions.py`, `epicure.py` |
| Domain | Cooking request, questions, answers, status and rules | `domain/requests.py`, `domain/clarification.py`, `domain/recommendations.py`, `domain/rule_planner.py` |
| Services | Planning orchestration, answer updates, concurrency | `services/clarification_service.py`, `hybrid_planner.py`, `answers.py`, `store.py` |
| Application LLM | Async provider interface, fake provider, bounded retries | `llm/client.py` |
| Recipe access | Parameterized SQL search, exact vector search, RRF fusion and document lookup | `recipes/repository.py`, `search.py`, `vector_search.py` |
| Ingestion | Source normalization, extraction, validation and loading | `recipes/import_data.py`, `adapters/`, `llm_batch.py`, `llm_sched.py`, supporting modules |
| Epicure | Load pinned vocabulary/vectors and calculate neighbors (core/cooc/chem) | `tools/epicure.py` |
| Agent tools (M3 Phase 2 + Phase 4 + Phase 5 part 2) | Typed registry, recipe search, Epicure pairings/substitutions, scaling/conversion, technique search, server-bound web search | `tools/registry.py`, `search_tools.py`, `epicure_tools.py`, `measure_tools.py`, `technique_tools.py`, `stub_tools.py` (see `docs/tools.md`, `docs/techniques.md`) |
| Agent loop (M3 Phase 3 + Phase 7) | Bounded hand-written loop, Epicure-by-default, ask/resume, select/plan, validation last gate, SSE stream | `agent/loop.py`, `agent/validate.py`, `api/agent.py` (see `docs/agent.md`, `docs/adr/0002-agent-loop.md`) |
| Web UI (M3 Phase 6) | Static page at `/ui` (no build): message box, streamed stages, options/plan/technique/web cards, question answering, toggle off by default (backend enforces) | `web/` (`index.html`, `demo.html`, `js/`, `styles.css`; see `docs/ui-walkthrough.md`) |
| Infrastructure | Settings, database engine, planning events | `config.py`, `db.py`, `obs/clarification.py` |

Development-only tools are under `scripts/`:
- dataset tools in `scripts/datasets/`;
- the embedding backfill CLI in `scripts/embeddings/embed.py`;
- the technique corpus CLI in `scripts/techniques/` (fetch, load, embed, eval baseline);
- the retrieval evaluation harness in `scripts/retrieval_eval/`.

Production code does not import them. Tests live under `tests/`. `evals/`
holds the frozen retrieval evaluation inputs, the measured full-text
baseline, the Phase 6 development additions and freeze record, and the
recommendation review specs. Recipe-filled results stay local and ignored.

Retrieval measurements so far:
- **Full-text baseline (Phase 2):** Recall@5 0.408 and MRR@5 0.446 over 27
  relevant-labeled units at grade 2. Incomplete judgments and held-out
  exposure limit the claim. See the
  [corrected baseline](../../evals/results/phase1/baseline_fulltext_corrected.md).
- **Phase 6 blind comparison:** 20 adversarial requests, owner-judged.
  - Vector search with a 0.66 cosine-distance cutoff (`vector_c`) won under
    the frozen rule.
  - HitRate@5 was 0.812 against full-text's 0.438.
  - The cutoff returned nothing on all four requests meant to find nothing.
  - The default was not changed. See the [scoreboard](../scoreboard.md) and
    [ADR 0001](../adr/0001-retrieval-default.md).

## 3. Deployment and lifetime

```mermaid
flowchart LR
    Host["Developer terminal"] -->|"localhost:8000"| API["API container or local uvicorn"]
    Host -->|"localhost:5432"| DB["PostgreSQL 17 + pgvector container (digest-pinned)"]
    API -->|"db:5432 in Compose; localhost locally"| DB
    DB --> Disk[("backend_postgres_data volume")]
    API --> RAM[("Clarification state in process memory")]
    API --> Cache[("huggingface_cache volume in Compose")]
```

- Compose runs `api` and `db`; CLI ingestion is a separate operator action.
- `compose.yaml` pins `pgvector/pgvector:pg17-trixie` by digest. Migration
  004 created `vector` objects, so the stock `postgres:17` image can no
  longer start this database. Never swap the image back on its own.
- PostgreSQL and cached model files survive container replacement through named volumes.
- Clarification state does **not** survive an API restart (in-memory store,
  512-request cap, single worker). Agent sessions **do** survive a restart:
  Phase 1 stores them in Postgres (`sessions` / `session_events`,
  migration `005`); see [sessions](../sessions.md).
- The clarification store caps retained requests at 512 and removes associated groups on eviction.
- App startup creates the engine, services and store. It initializes the application
  provider when enabled; shutdown closes that client and disposes the engine.
- `/health/live` reports process availability; `/health/ready` checks PostgreSQL
  connectivity. Neither certifies model availability, corpus quality, or conversation persistence.

## 4. Independent switches

| Switch | Enables | Does not enable |
|---|---|---|
| `LLM_ENABLED` | Optional interactive clarification planning | Batch ingestion or recipe generation |
| `LLM_RECOMMENDATION_ENABLED` | Grounded recommendation selection (Phase 3) | Clarification planning or batch ingestion |
| `LLM_INGESTION_ENABLED` | Explicit ingestion extraction calls | Interactive clarification or recommendations |
| `EPICURE_ENABLED` | Local Epicure pairing capability | Dietary certification or automatic recommendation generation |

Defaults are disabled in code.

Embedding and retrieval-mode settings are validated at load and, since
Milestone 3 Phase 2, wired to the retrieval request path (ADR 0001
steps 1–2 and 5; see `docs/tools.md`): `POST /api/v1/retrieval/search`
reads `RETRIEVAL_MODE`, `RETRIEVAL_VECTOR_CUTOFF`,
`RETRIEVAL_FULLTEXT_GATE`, `RETRIEVAL_RRF_K`,
`RETRIEVAL_VECTOR_CANDIDATES` plus the embedding model/dimension, and
the query provider starts only when `EMBEDDINGS_ENABLED` is set (with
a model/dimension check). Code default stays `fulltext`, so
default-settings behavior is unchanged (zero embedding calls). The
recommendations path intentionally keeps full-text (ADR step 4 open).
The mode, cutoff, gate and fallback remain parameters of
`retrieve_for_group` and `retrieve_with_mode`, which the evaluation
harness and tests also use. The embedding backfill CLI takes its own
explicit options and budget. A request can also set `use_llm=false` for rule-only
clarification. These are configuration defaults, not a claim about local `.env` values.
Clarification provider limits use `LLM_APP_*`; recommendations use `LLM_REC_*`
and `REC_*`; ingestion limits use separate settings.
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
| Source-grounded recommendations | Implemented backend-only (Phase 3): `POST /api/v1/recommendations` selects one source recipe with server-rendered content, deterministic validation, and Epicure consultation or a recorded skip/degraded outcome; see `docs/recommendations.md` |
| Epicure inside recommendations | Implemented: early consultation with canonical ingredients via cached assets (`CachedEpicureAdapter`), distinct outcomes (consulted/skip/disabled/unavailable/unmapped/insufficient context), opt-in `get_recipe` tool mode |
| Substitution verification | Limited checks; suggestions remain unverified, not certified equivalents |
| Agent sessions + cooking phases | Implemented (Milestone 3, Phase 1): Postgres `sessions` + append-only `session_events` (migration `005`), phase table with `recommend -> plan` skip-select, create/read/permission endpoints; budgets stored (12 tool calls, 12 steps) and enforced by the loop. See [sessions](../sessions.md) |
| Durable clarification conversations | Not implemented (in-memory store stays; sessions link by ID; migration path in [sessions](../sessions.md)) |
| Recipe embeddings / pgvector | Implemented (Phase 5): migration 004 adds pgvector and `recipe_embeddings`. There is one `text-embedding-3-small` 1536-dimension vector per recipe, and search is an exact cosine scan with no HNSW index. `search_vector` is still the separate PostgreSQL full-text column |
| Vector / hybrid retrieval | Retrieval endpoint wired (M3 Phase 2, ADR 0001 steps 1–2/5): `RETRIEVAL_MODE` and friends reach `retrieve_for_group`; provider starts only when `EMBEDDINGS_ENABLED`. Code default stays `fulltext` (zero embedding calls); recommendations keep full-text (step 4 open). Vector/hybrid measured in Phase 6; flipping the default still needs owner approval |
| Typed tool layer (M3 Phase 2 + Phase 4 + Phase 5 part 2) | Implemented: 10 typed tools (search/get, 3 Epicure variants + substitutions, scale/convert, technique search, server-bound web search) with server-set 10 s timeout, typed errors + `next_action`, and `session_events` logging; see `docs/tools.md`, `docs/techniques.md` |
| Technique corpus (M3 Phase 4) | Implemented and applied to the app DB: 34 approved documents, 253 chunks, each with an embedding (migrations 006 full-text, 007 vector). `search_techniques` runs full-text by default, with vector as an option, and returns attribution. Plan and cook `technique_refs` are validated and stored as evidence. Frozen 16-case eval: full-text HitRate@5 0.625, MRR 0.594. See `docs/techniques.md` |
| Bounded agent loop (M3 Phase 3 + Phase 7) | Implemented. See [the agent system](agent-system.md), `docs/agent.md` and ADR 0002. A hand-written loop over the registry: native function calling, parallel calls recorded in call order, a CAS write and an event per step. Server-set budgets: 12 steps, 12 calls, 60k input tokens, 90 s. Stable stops with `next_action`; Epicure by default; answers, select and SSE endpoints. Phase 7 offline harness: v8, 43 cases (`docs/agent-scoreboard.md`). The live evaluation ran 11 sessions; the owner reviewed them at checkpoint C (`evals/phase7_agent/CHECKPOINT_C.md`) |
| Recipe rewriting, scaling, web search | Recommendations select and render stored sources; scaling and conversion are deterministic tools (unknown stays unknown). Plans that change a source's method are labelled `model_adaptation`. Web search is permission-gated in the backend, in server-bound `search_web`: off means denied with no slot; on means an atomic slot claim (at most 3 per session), then one bounded hosted sub-request. Answers are discovery-only: cited pointers and page descriptions, never a cooking method |
| Streaming | Implemented (Phase 4): `POST /api/v1/recommendations/stream` shares the recommendation service via a stage hook; versioned stage/final/error events, bounded duration/events, disconnect cancellation |
| Complete telemetry | Implemented (Phase 4): correlated clarification + recommendation events with real ids, stage timings, per-turn usage and estimated cost from the model registry (gpt-6-luna); one event per run including cancelled runs; no message/recipe/secret logging |

Important limits: conflict checks are keyword-based; source quote matching for
inferred fields is substring-based, not semantic proof. Planning and
recommendation event logging is complete per request (rule-only and error
paths emit with real group ids; recommendation telemetry covers success,
insufficient, error, and cancelled stream outcomes).

## 6. How to verify and navigate

`make check` runs Ruff, formatting, mypy and tests/evals. Default model tests use
fake providers. PostgreSQL integration tests use disposable databases when available.
No live model reliability claim follows from fake-provider tests.

The pgvector tier (`PGVECTOR_TEST_URL`, `tests/test_embeddings_pg.py`) needs
a disposable database whose name contains `test`, `disposable` or `check`.
The tier migrates and seeds that database itself (three recipes), so a
fresh `pgvector/pgvector` container is enough:

```sh
docker run -d --rm --name cc-pgv-test -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t \
  -e POSTGRES_DB=cc_disposable_test -p 127.0.0.1:55439:5432 \
  pgvector/pgvector:pg17-trixie@sha256:724a4041afdb1750446e3f6b5cfa8f3b0ac5a2cf538ddfa6bfee4f94c2fa85c6
PGVECTOR_TEST_URL=postgresql+psycopg://t:t@127.0.0.1:55439/cc_disposable_test \
  uv run pytest -q tests/test_embeddings_pg.py
docker stop cc-pgv-test
```

- [The agent system](agent-system.md)
- [Agentic development workflow](agentic-development.md)
- [Agent loop contracts](../agent.md), [tools](../tools.md), [sessions](../sessions.md)
- [Agent scoreboard](../agent-scoreboard.md) and [checkpoint C packet](../../evals/phase7_agent/CHECKPOINT_C.md)
- [Clarification contracts and HTTP examples](../clarification.md)
- [Retrieval evidence summaries](../retrieval.md)
- [Phase 6 retrieval scoreboard](../scoreboard.md)
- [ADR 0001: retrieval default](../adr/0001-retrieval-default.md)
- [Grounded recommendations and Epicure](../recommendations.md)
- [Food.com import runbook](../recipe-ingestion.md)
- [Hybrid ingestion and completed local migration](../hybrid-ingestion.md)
- [Database schema and ingestion diagrams](data-and-ingestion.md)
- [Request lifecycle and concurrency diagrams](request-flows.md)
