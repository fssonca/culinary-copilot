# Culinary Copilot

Week 1 backend foundation for the Epicure learning project. Python 3.13, FastAPI,
PostgreSQL 17, uv, and Docker Compose. Hybrid recipe ingestion supports opt-in
LLM extraction; application recipe generation is not implemented. API keys are
not needed to start the backend or run default tests.

## Run with Docker

```sh
cp .env.example .env
# Edit .env if needed, then:
docker compose up --build -d --wait
curl --fail http://localhost:8000/health/ready
```

Open [Swagger UI](http://localhost:8000/docs). `GET /health/live` checks the API;
`GET /health/ready` checks the database and returns 503 when unavailable.

```sh
docker compose logs -f api
docker compose down
```

The backend uses its own `backend_postgres_data` volume, preserving any older scaffold database.
Postgres and model caches persist in named volumes. `down` retains them;
`docker compose down -v` deletes them. Default credentials are for local development.
If changing credentials, keep `DATABASE_URL` aligned for local Python. URL-encode
special characters in the URL; Compose's generated URL assumes URL-safe credentials.
Changing Postgres environment variables does not update an already initialized volume.

## Local Python development

Install [uv](https://docs.astral.sh/uv/), then:

```sh
cp .env.example .env  # Only on first setup; preserve an existing .env.
uv sync --locked
docker compose up -d db
make dev
```

`make check` runs Ruff, strict mypy, offline tests and disposable-Postgres tests
when PostgreSQL is available. CI also builds and starts the
Docker stack and checks database readiness. Keep `.env` and downloaded models out of Git.

## Epicure Core (optional)

Set `EPICURE_ENABLED=true` in `.env`, then recreate the API:

```sh
docker compose up -d --force-recreate api
curl --fail 'http://localhost:8000/api/v1/pairings?ingredient=chicken&k=5'
```

The first pairing request downloads pinned vocabulary and SafeTensors files from
[Kaikaku/epicure-core](https://huggingface.co/Kaikaku/epicure-core), then computes cosine
neighbors locally on CPU. Subsequent requests reuse the loaded matrix; downloads
persist in the cache. No GPU or hosted inference provider is needed. `HF_TOKEN` is
optional for this public repository. Disabled requests return 503; unknown ingredients
return 422. Use canonical English names (spaces are converted to underscores).

The adapter supports nearest neighbors only. Similarity scores are not probabilities;
results do not enforce dietary constraints or allergies. Advanced Epicure operators
and constraint filtering are future work.

Model data: **CC BY 4.0**, attribution to Jakub Radzikowski and Josef Chen,
KAIKAKU.AI, [Epicure paper](https://arxiv.org/abs/2605.22391).
The project computes normalized cosine similarity over the original vectors without
modifying or committing the upstream assets. Revision is configured in `.env.example`.

## OpenAI integration

Add `OPENAI_API_KEY` to `.env` when ready. The configured default is
[`gpt-5-nano`](https://developers.openai.com/api/docs/models/gpt-5-nano);
[`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna) is an
alternative configuration. Adding a key alone enables nothing: ingestion calls
require `LLM_INGESTION_ENABLED=true` (see [hybrid ingestion](docs/hybrid-ingestion.md)
for explicit enablement and budgets), and hybrid clarification planning requires
the separate `LLM_ENABLED=true` (see [clarification backend](docs/clarification.md)).
Default tests are key-free and offline either way.

## Architecture

Start with the [current architecture guide](docs/architecture/README.md) for system,
request-flow, concurrency, ingestion and database diagrams. It distinguishes
implemented behavior from planned integrations.

## Structure and next steps

```text
src/culinary_copilot/
  api/       FastAPI app, health, pairing, recipe, clarification and retrieval endpoints
  domain/    Pydantic cooking-request schema, clarification contracts, rule planner
  llm/       Application provider boundary (fake + async OpenAI)
  services/  Hybrid planning, answer processing, in-memory clarification store
  retrieval/ Phase 1 ready-request mapping plus bounded evidence summaries
  obs/       Clarification planning events
  tools/     Opt-in Epicure Core adapter
  recipes/   Normalization, import, migrations and offline retrieval
  config.py  Environment settings (secrets masked in repr)
  db.py      SQLAlchemy connection pool and readiness check
 tests/      Offline backend checks
 evals/      Existing evaluation scaffold
```

The backend and recipe data foundation are implemented. The local application
migration is complete; new environments require explicit database setup and import.
Hybrid clarification planning (rule + bounded LLM questions, answer processing,
and the `/api/v1/clarification` endpoints) is implemented backend-only; see
[clarification backend](docs/clarification.md). Phase 1 retrieval
(`POST /api/v1/retrieval/search`: ready-request mapping plus bounded
evidence summaries, AI-proposed review packet) is implemented and repaired
(dish eligibility vs pantry ranking, shared duration policy, current-group
contract); see [retrieval guide](docs/retrieval.md). Labels are AI-proposed
until human-reviewed; no definitive retrieval score is published. The
official Phase 2 baseline waits on label calibration and the approved
Food.com search rebuild. Milestone 2 adds recipe embeddings, structured
sourced responses, pgvector and measured retrieval evaluations.
Epicure Core is available; Cooc/Chem remain deferred. Agent iteration and generated
cooking plans are not implemented.

## Recipe foundation and next milestone

- [Milestone 1 assessment and recipe import review/runbook](docs/recipe-ingestion.md)
- [Hybrid ingestion and completed local migration](docs/hybrid-ingestion.md)
- [Historical corpus workstreams](docs/corpus-workstreams.md)
- [jojogo9 dataset audit and hold decision](docs/jojogo9-provenance-coverage.md)

`uv run import-recipes` creates a local preview by default. Database writes require
`--write`; first-time schema setup also requires `--apply-schema`. Review the runbook
before running it. Recipe discovery is available after import. No LLM calls are needed.

```sh
# Combined search across both imported datasets (default):
curl 'http://localhost:8000/api/v1/recipes?q=garlic'
# Single-dataset search:
curl 'http://localhost:8000/api/v1/recipes?q=garlic&dataset_id=odunola%2Ffoodie'
# Dataset-qualified lookup (exact pair, 404 on miss with no fallback):
curl 'http://localhost:8000/api/v1/recipes/000038?dataset_id=AkashPS11%2Frecipes_data_food.com'
```

Search defaults to the combined corpus (`AkashPS11/recipes_data_food.com` +
`odunola/foodie`); pass `dataset_id` to scope results to one dataset.
Ingredient, `max_minutes`, and `limit` filters apply identically to combined
and single-dataset search, and every hit carries `dataset_id`/`source_id`.
Canonical identity is the `(dataset_id, source_id)` pair: with `dataset_id`,
lookup matches that pair exactly and never falls back. Without it, legacy
Food.com-first lookup is preserved (including `000038`); new clients should
supply `dataset_id`. Alias IDs remain provenance metadata and do not resolve
as independent API rows.
