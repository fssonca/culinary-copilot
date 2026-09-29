# Data storage and ingestion

Part of the [current architecture](README.md). SQL definitions live in
`src/culinary_copilot/recipes/migrations/`; the migration ledger is created by
`apply_migrations()` in `import_data.py`.

## 1. One database, one shared recipe table

Both Food.com and Foodie occupy `public.recipes` in the same PostgreSQL database.
`dataset_id` identifies the source; it does not select a different database/schema.

```mermaid
erDiagram
    RECIPE_IMPORTS ||--o{ RECIPES : supplies
    RECIPE_IMPORTS ||--o{ RECIPE_QUARANTINE : records
    RECIPES ||--o{ RECIPE_EMBEDDINGS : "embedded as"
    RECIPE_IMPORTS {
        text id PK
        text dataset_id
        text revision
        text checksum
        text normalizer_version
        text vocabulary_checksum
        text dataset_url
        jsonb report
        timestamptz imported_at
    }
    RECIPES {
        text dataset_id PK
        text source_id PK
        text import_id FK
        text title
        float total_minutes
        float servings
        text_array ingredient_names
        jsonb document
        text search_text
        tsvector search_vector
        text search_document_version
    }
    RECIPE_QUARANTINE {
        text import_id PK, FK
        int row_number PK
        text reason
        jsonb raw
        text source_id
        text status
        text verdict
        jsonb problems
    }
    RECIPE_SCHEMA_MIGRATIONS {
        text version PK
        text checksum
    }
    RECIPE_EMBEDDINGS {
        text dataset_id PK, FK
        text source_id PK, FK
        text model PK
        int dimension PK
        text renderer_version PK
        text chunking_version PK
        int chunk_index PK
        text embedded_text_hash
        vector embedding
        text import_id
        timestamptz created_at
    }
    EMBEDDING_RUNS {
        text run_id PK
        text model
        int dimension
        text renderer_version
        text chunking_version
        text status
        int reserved_tokens
        int used_tokens
    }
    SESSIONS {
        text id PK
        int revision
        text current_phase
        text clarification_request_id
        text clarification_group_id
        jsonb constraints
        jsonb confirmed_answers
        jsonb unresolved_questions
        text epicure_outcome
        text epicure_skip_reason
        jsonb suggestions
        jsonb selected_dish
        jsonb cooking_plan
        jsonb evidence
        bool internet_search_allowed
        int tool_calls_remaining
        int steps_remaining
    }
    SESSION_EVENTS {
        bigint id PK
        text session_id FK
        int seq
        text event_type
        jsonb payload
    }
    SESSIONS ||--o{ SESSION_EVENTS : logs
```

`(dataset_id, source_id)` is the composite recipe key. Quarantine uses
`(import_id, row_number)`; its dataset comes through the import record.
The migration ledger is independent of the recipe relationships.

Embedding rows cascade-delete with their recipe. Their key includes the
model, dimension, renderer and chunking versions, so vectors from
different versions can coexist; vector search reads only the current
versions. `embedding_runs` is the backfill CLI's run and budget ledger and
has no foreign key. `float` and
`text_array` above abbreviate SQL `double precision` and `text[]`.

### Columns versus JSON

Searchable columns support fast filtering. `document` is the complete structured
recipe: ingredients, instructions, raw fields, provenance, and available quality
metadata. The JSON documents are not perfectly uniform across import generations:
older Food.com documents lack some newer Foodie capability and quality fields.
Consumers must handle absence as unknown, not assume validation succeeded.

`search_vector` is a generated PostgreSQL **tsvector**, an indexable representation
of words. It is not an embedding. GIN indexes support full-text and ingredient-array
queries; a duration index supports time filtering. Since migration 004 the
database also carries the pgvector extension and a `recipe_embeddings` table
(one 1536-dimensional `text-embedding-3-small` vector per recipe, renderer
version 1); vector search is an exact scan (no HNSW index).

### Migration history

- **001:** imports, recipes, quarantine and indexes.
- **002:** search-document renderer version column.
- **003:** quarantine source ID, status, verdict and problem details.
- **004:** pgvector extension, `recipe_embeddings` and `embedding_runs`.
- **005:** agent sessions + append-only `session_events` (Milestone 3,
  Phase 1; plain Postgres, no extension). Rehearsed on disposable
  databases only; not yet applied to the application database in this
  change. See [sessions](../sessions.md).
- **006:** technique documents + section chunks with tsvector full-text
  (Milestone 3, Phase 4; plain Postgres, applies on both images).
- **007:** technique chunk embeddings, `requires-extension: vector`
  (Phase 4; skipped with a reason on stock `postgres:17`, where 006
  works on its own). Both rehearsed on disposable databases only;
  not applied to the application database. See
  [techniques](../techniques.md).

Applied migration checksums are preserved; changes require new SQL migrations.
Startup does not run migrations. Explicit ingestion/load commands can apply them.

Operational note: `compose.yaml` pins the pgvector image by digest
(`pgvector/pgvector:pg17-trixie@sha256:724a…c2fa85c6`, the image the
database already runs). Reverting to stock `postgres:17` is unsafe now
that 004 is applied — the stock image cannot load `vector` objects and
the database will not start. The former
`compose.pgvector.override.yaml` was superseded by the pin and removed;
do not reintroduce an unpinned image.

### Recorded local migration snapshot

The completed migration report records 1,216 Food.com + 14,817 Foodie recipes
(**16,033 total**) and 12 + 431 quarantine rows (**443 total**). These are historical
local counts, not an automatic seed or a live count rechecked for this documentation.
See [the migration runbook section](../hybrid-ingestion.md#completed-foodie-migration-2026-09-23).

## 2. Two ingestion paths

```mermaid
flowchart TD
    Foodcom["Pinned Food.com CSV"] --> Import["import_data: checksum and deterministic normalization"]
    Import --> Preview["Preview and quality artifacts"]
    Preview -->|"explicit write"| Replace["Transactional dataset snapshot replacement"]
    Replace --> PG[("PostgreSQL")]
    Foodie["Pinned Foodie source text"] --> Parse["Deterministic adapter"]
    Parse --> Route{"Routing decision"}
    Route -->|"deterministic accept"| Final["Finalize and merged-evidence checks"]
    Route -->|"needs LLM"| Prepare["Prepare JSONL and manifest"]
    Route -->|"quarantine"| Hold["Held source and reasons"]
    Prepare --> Submit["Explicit Batch submission"]
    Submit --> Collect["Status and collect saved responses"]
    Collect --> Validate["Validate evidence and bounded extraction"]
    Validate --> Final
    Validate -->|"eligible failure; attempts remain"| Retry["Retry child run"]
    Retry --> Submit
    Retry --> Reconcile["Reconcile child outcomes into parent"]
    Reconcile --> Final
    Final --> Ready["Ready and terminal dispositions"]
    Hold --> Ready
    Ready --> Load["Explicit transactional load; upsert recipes and held records"]
    Load --> PG
```

This is a lifecycle diagram, not an automatic promise that every arrow runs.
Model outputs are proposals; Python owns validation, merged evidence, capabilities,
retry eligibility, and the decision to admit a record. Partial records can remain
useful evidence with scaling and completion restricted. Pending work is distinct
from terminal rejection; full loads block on pending records unless partial loading
is explicitly selected.

- **Food.com importer:** preview by default; explicit write replaces that dataset's
  current accepted snapshot in a transaction. It does not replace other datasets.
- **Foodie Batch loader:** upserts ready rows and terminal held records. It does
  not use Food.com's snapshot-replacement policy.
- **Scheduler:** wraps the Batch lifecycle with persisted queue state, slot limits,
  spend reservations, reviews, stop rules, retries and reconciliation. It is a CLI
  tool, not an always-running API service. Scope and limits are run configuration.
- **Cache:** keys capture interpretation inputs; validator/merge versions protect
  reuse. Revalidation of saved responses can be offline, without new paid calls.
- **Exact deduplication:** `exact_duplicates.py` groups identical raw source text,
  preserving source aliases and alternate interpretations. It was used in migration
  preparation; it is not automatically wired into every ordinary ingestion command.

## 3. What persists where

| Location | Contents | Lifetime |
|---|---|---|
| PostgreSQL volume | Canonical recipes, quarantine, import reports, migration ledger, recipe embeddings and embedding-run ledger, agent sessions + session event log (005) | Durable across container restarts |
| API process memory | Cooking requests, questions, answers, confirmations, revisions | Lost on restart; bounded eviction |
| Ignored `data/` | Batch manifests, responses, reviews, migration package and backup, Phase 6 evaluation outputs (raw retrieval, judging packet, judgments, scores) | Local files, not Git or conversation storage |
| Model cache | Pinned Epicure vocabulary and vectors | Local cache / Docker volume |
| Git repository | Code, migrations, tests, selected fixtures and maintained docs | Versioned; no runtime dataset corpus |

Foodie exact duplicates retain `source_records` and `duplicate_interpretations`
in the canonical JSON document; the migration's `aliases.json` also maps original
IDs. Those aliases do not create separate API-addressable recipe rows.

Boundary/structural cases remain outside the admitted scope or held for later work.
Quarantine is excluded from ordinary recipe search. There is no automatic stronger-model
fallback or full-corpus expansion merely because a prior evaluation used one.

## 4. Operational separation

Interactive clarification can read recipe metadata, but does not import or write recipes.
Ingestion writes occur only through explicit operator commands. Regression tests use
saved/synthetic data and fake providers; integration tests write only disposable databases.

Detailed commands, limits and recovery evidence belong in the maintained
[Food.com](../recipe-ingestion.md) and [hybrid](../hybrid-ingestion.md) runbooks.

## 5. Runtime reads, quarantine history and review records

Recommendation search and exact lookup read `recipes`, not `recipe_quarantine`.
A quarantined-only identity is never offered. An accepted version can still be
served when quarantine contains an older rejected version or a duplicate import
row. The Foodie loader upserts accepted rows; a later rejection does not delete an
earlier accepted row. Disposable-Postgres tests cover these boundaries.

Phase 3 adds no database tables or migrations. Review proposals and owner decisions
live under `evals/phase3_review/`; they are not runtime enrichment overlays.
Acceptance does not alter recipe documents or capabilities. Source-filled packets
and provider outputs are local ignored files; the specs and hash-only manifest are
versioned. PostgreSQL remains the source of runtime recipe facts, and clarification
state remains process-local memory.

Milestone 3, Phase 1 adds migration `005` (`sessions`, append-only
`session_events`). Sessions link to clarification by ID and survive restarts;
clarification itself stays in memory (see [sessions](../sessions.md) for the
later migration path).

Phase 6 adds no tables or migrations and writes nothing to the application
database. Its evaluation only read recipes and `recipe_embeddings`. The
freeze record (`evals/results/phase6/freeze.json`) and the development
cases are versioned. Recipe-filled outputs stay in ignored `data/phase6/`.
Those files are identified by the hashes listed in the
[scoreboard](../scoreboard.md).
