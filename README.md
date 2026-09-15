# Culinary Copilot

Week 1 backend foundation for the Epicure learning project. Python 3.13, FastAPI,
PostgreSQL 17, uv, and Docker Compose. No LLM calls are implemented; OpenAI settings
are reserved for the next step. API keys are not needed to start or test the backend.

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

`make check` runs Ruff, strict mypy, and offline tests. CI also builds and starts the
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

## Future OpenAI integration

Add `OPENAI_API_KEY` to `.env` when ready. The configured default is
[`gpt-5-nano`](https://developers.openai.com/api/docs/models/gpt-5-nano);
[`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna) is an
alternative. Adding a key does not enable requests: the provider implementation is
intentionally deferred along with streaming, retries, structured generation, and usage tracking.

## Structure and next steps

```text
src/culinary_copilot/
  api/       FastAPI app, health and pairing endpoints
  domain/    Pydantic cooking-request schema
  llm/       Reserved provider package
  tools/     Opt-in Epicure Core adapter
  recipes/   Reserved Week 2 ingestion/retrieval package
  config.py  Environment settings (secrets masked in repr)
  db.py      SQLAlchemy connection pool and readiness check
 tests/      Offline backend checks
 evals/      Existing evaluation scaffold
```

This is the boilerplate portion of Week 1, not the full week's implementation.
Next: request clarification, async OpenAI client, structured outputs, and evaluation
cases. Week 2 adds recipe tables and migrations, ingestion, then retrieval/pgvector.
There are no application tables or migrations yet because no persistence feature is
implemented. No recipe retrieval, agent loop, or generated cooking plans are exposed.
