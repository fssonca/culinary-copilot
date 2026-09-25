-- 004: recipe-text embeddings (Phase 5, pgvector activation only).
-- requires-extension: vector
--
-- Additive only; never edit 001-003. Creates the pgvector extension,
-- chunk-embedding tables with a cascading FK to recipes, and an
-- embedding-runs ledger for resumable jobs. The embedding column uses a
-- dimension-agnostic ``vector`` type so any registry model applies; the
-- configured dimension is enforced by CHECK against the supported set
-- (text-embedding-3-small 1536, text-embedding-3-large / ada-002) and by
-- application startup validation (query and corpus must share one
-- model/dimension). Full-text search keeps working when this migration is
-- skipped on stock postgres:17 (see import_data.apply_migrations: vector-
-- tagged migrations are skipped with a reason when the extension is
-- unavailable, never partially applied). Activation requires the pgvector
-- image override (compose.pgvector.override.yaml); SQL rollback alone
-- cannot undo a container/image change.
-- Migration 004 has never been applied to the application database
-- (verified: recipe_schema_migrations holds 001-003 only), so it may still
-- be corrected here. Only disposable rehearsal databases carry earlier
-- revisions of 004; they are rebuilt, never migrated forward.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS recipe_embeddings (
    dataset_id text NOT NULL,
    source_id text NOT NULL,
    model text NOT NULL,
    dimension integer NOT NULL,
    renderer_version text NOT NULL,
    chunking_version text NOT NULL,
    chunk_index integer NOT NULL,
    embedded_text_hash text NOT NULL,
    embedding vector NOT NULL,
    import_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, source_id, model, dimension, renderer_version, chunking_version, chunk_index),
    FOREIGN KEY (dataset_id, source_id) REFERENCES recipes(dataset_id, source_id) ON DELETE CASCADE,
    CONSTRAINT recipe_embeddings_dimension_check CHECK (dimension IN (1536, 3072)),
    CONSTRAINT recipe_embeddings_vector_length_check CHECK (vector_dims(embedding) = dimension),
    CONSTRAINT recipe_embeddings_chunk_check CHECK (chunk_index >= 0)
);

CREATE INDEX IF NOT EXISTS recipe_embeddings_lookup
    ON recipe_embeddings (dataset_id, source_id);

CREATE TABLE IF NOT EXISTS embedding_runs (
    run_id text PRIMARY KEY,
    model text NOT NULL,
    dimension integer NOT NULL,
    renderer_version text NOT NULL,
    chunking_version text NOT NULL,
    status text NOT NULL DEFAULT 'running',
    reserved_tokens integer NOT NULL DEFAULT 0,
    used_tokens integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
