-- 006: technique corpus documents + chunks (Phase 4, plain PostgreSQL).
--
-- Additive only; never edit 001-005. Plain PostgreSQL on purpose: no
-- extension requirement, so this migration applies on BOTH the stock
-- postgres:17 image and the pgvector image. The vector side lives in
-- 007 (requires-extension: vector) and activates only where pgvector
-- exists. Full-text technique search works on stock postgres:17 with
-- 006 alone.
CREATE TABLE IF NOT EXISTS technique_documents (
    doc_id text PRIMARY KEY,
    title text NOT NULL,
    final_title text NOT NULL DEFAULT '',
    topic text NOT NULL DEFAULT '',
    url text NOT NULL,
    publisher text NOT NULL,
    licence text NOT NULL,
    licence_url text NOT NULL,
    attribution_text text NOT NULL,
    retrieval_date_utc text NOT NULL,
    revision_id text NOT NULL,
    sha256_raw text NOT NULL,
    sha256_normalized text NOT NULL,
    words integer NOT NULL DEFAULT 0,
    bytes integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS technique_chunks (
    doc_id text NOT NULL REFERENCES technique_documents(doc_id) ON DELETE CASCADE,
    chunk_id integer NOT NULL,
    section text NOT NULL DEFAULT '',
    chunk_text text NOT NULL,
    search_vector tsvector,
    PRIMARY KEY (doc_id, chunk_id),
    CONSTRAINT technique_chunks_chunk_check CHECK (chunk_id >= 0)
);

CREATE INDEX IF NOT EXISTS technique_chunks_search_idx
    ON technique_chunks USING GIN (search_vector);
