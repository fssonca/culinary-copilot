-- 005: agent session state + append-only session event log (Phase 1, Milestone 3).
--
-- Plain PostgreSQL only: no extension requirement, no vector objects, so this
-- migration applies on stock postgres:17 as well as on the pgvector image.
-- Additive only; never edit 001-004. The in-memory clarification store stays
-- for the existing clarification, retrieval and recommendation endpoints;
-- sessions link to clarification requests/groups by ID rather than copying
-- their state (see docs/sessions.md for the migration path).
--
-- Tables:
-- - sessions: one row per agent session. Holds constraints, confirmed
--   answers, unresolved questions, Epicure outcome or skip reason,
--   suggestions, selected dish, cooking plan, current phase,
--   evidence/source references, internet-search permission (default off)
--   and remaining tool budget. Revision column supports compare-and-set
--   updates with the same 409 semantics as clarification.
-- - session_events: append-only log. Application code only INSERTs and
--   SELECTs; UPDATE/DELETE are rejected by trigger (see below).
--
-- Rollback (manual, documented in docs/sessions.md; never automatic):
--   DROP TRIGGER IF EXISTS session_events_no_mutation ON session_events;
--   DROP FUNCTION IF EXISTS prevent_session_events_mutation();
--   DROP TABLE IF EXISTS session_events;
--   DROP TABLE IF EXISTS sessions;
--   DELETE FROM recipe_schema_migrations WHERE version = '005';
-- This migration has never been applied to the application database in this
-- change (rehearsed on disposable databases only).

CREATE TABLE IF NOT EXISTS sessions (
    id text PRIMARY KEY,
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    current_phase text NOT NULL DEFAULT 'discover'
        CHECK (current_phase IN (
            'discover', 'clarify', 'research', 'recommend',
            'select', 'plan', 'cook', 'plate'
        )),
    clarification_request_id text,
    clarification_group_id text,
    constraints jsonb NOT NULL DEFAULT '{}'::jsonb,
    confirmed_answers jsonb NOT NULL DEFAULT '[]'::jsonb,
    unresolved_questions jsonb NOT NULL DEFAULT '[]'::jsonb,
    epicure_outcome text,
    epicure_skip_reason text,
    suggestions jsonb NOT NULL DEFAULT '[]'::jsonb,
    selected_dish jsonb,
    cooking_plan jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    internet_search_allowed boolean NOT NULL DEFAULT FALSE,
    tool_calls_remaining integer NOT NULL DEFAULT 12 CHECK (tool_calls_remaining >= 0),
    steps_remaining integer NOT NULL DEFAULT 8 CHECK (steps_remaining >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sessions_clarification_request
    ON sessions (clarification_request_id);

CREATE TABLE IF NOT EXISTS session_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id text NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq integer NOT NULL CHECK (seq >= 1),
    event_type text NOT NULL CHECK (char_length(event_type) BETWEEN 1 AND 100),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS session_events_session_seq
    ON session_events (session_id, seq);

-- Append-only enforcement: the application never UPDATEs or DELETEs events.
-- The trigger fails closed so an accidental write surfaces as an error
-- instead of silently rewriting history.
CREATE OR REPLACE FUNCTION prevent_session_events_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'session_events is append-only: % not allowed', TG_OP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS session_events_no_mutation ON session_events;
CREATE TRIGGER session_events_no_mutation
    BEFORE UPDATE OR DELETE ON session_events
    FOR EACH ROW EXECUTE FUNCTION prevent_session_events_mutation();
