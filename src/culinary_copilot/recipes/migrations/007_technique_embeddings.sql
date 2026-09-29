-- 007: technique chunk embeddings (Phase 4, pgvector activation only).
-- requires-extension: vector
--
-- Additive only; never edit 001-006. Creates the pgvector extension and
-- the technique chunk-embedding table with a cascading FK to
-- technique_chunks. Reuses the 004 column discipline: dimension-agnostic
-- ``vector`` type, CHECK against the supported registry set, and
-- ``vector_dims`` length enforcement, plus the full reusable identity
-- (model, dimension, technique renderer version, technique chunking
-- version, embedded-text hash). The ledger reuses ``embedding_runs``
-- from 004 (technique runs use a ``tech-`` run-id prefix).
--
-- Full-text technique search keeps working when this migration is
-- skipped on stock postgres:17 (see import_data.apply_migrations:
-- vector-tagged migrations are skipped with a reason when the extension
-- is unavailable, never partially applied). Activation requires the
-- pgvector image override (compose.pgvector.override.yaml); SQL
-- rollback alone cannot undo a container/image change. Never applied
-- to the application database in Phase 4 part 2 (disposable rehearsal
-- databases only).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS technique_embeddings (
    doc_id text NOT NULL,
    chunk_id integer NOT NULL,
    model text NOT NULL,
    dimension integer NOT NULL,
    renderer_version text NOT NULL,
    chunking_version text NOT NULL,
    embedded_text_hash text NOT NULL,
    embedding vector NOT NULL,
    import_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, chunk_id, model, dimension, renderer_version, chunking_version),
    FOREIGN KEY (doc_id, chunk_id) REFERENCES technique_chunks(doc_id, chunk_id) ON DELETE CASCADE,
    CONSTRAINT technique_embeddings_dimension_check CHECK (dimension IN (1536, 3072)),
    CONSTRAINT technique_embeddings_vector_length_check CHECK (vector_dims(embedding) = dimension),
    CONSTRAINT technique_embeddings_chunk_check CHECK (chunk_id >= 0)
);

CREATE INDEX IF NOT EXISTS technique_embeddings_lookup
    ON technique_embeddings (doc_id, chunk_id);
