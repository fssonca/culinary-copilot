-- Search retention purge (Phase 5, owner decision 8).
-- Owner-run only, never automatic. Tested on disposable databases only
-- (tests/test_phase5_search.py runs every statement in this file).
--
-- 1) Back up first, e.g.:
-- COPY (SELECT * FROM session_events WHERE <scope below>)
--   TO '/backup/search-events-YYYYMMDD.csv' CSV HEADER;
--
-- 2) Run this file as ONE transaction (psql -f). It DISABLEs the
-- append-only trigger, DELETEs expired rows, re-ENABLEs the trigger,
-- then COMMITs. Migration 005 installs session_events_no_mutation, a
-- BEFORE UPDATE OR DELETE trigger that raises on the first expired
-- row, so a plain DELETE cannot purge. ALTER TABLE is transactional:
-- any failure rolls back with the trigger still enabled.
-- Lock: ALTER TABLE takes ACCESS EXCLUSIVE on session_events (brief,
-- owner-run only). Privilege: run as the table owner.
--
-- Scope: the six search_* event types plus tool_call events for
-- search_web only (payload->>'tool' = 'search_web': recorded args and
-- result_facts such as hosts and titles). NOT purged: user_message,
-- agent_question and all other agent events — session data outside
-- the search retention decision; they persist.
--
-- RETENTION_DAYS = 90 (single value; change here):

BEGIN;

ALTER TABLE session_events DISABLE TRIGGER session_events_no_mutation;

DELETE FROM session_events
 WHERE (
   event_type IN (
     'search_slot_claimed',
     'search_requested',
     'search_results_retrieved',
     'evidence_evaluated',
     'search_outcome',
     'search_operations'
   )
   OR (event_type = 'tool_call' AND payload->>'tool' = 'search_web')
 )
 AND created_at < now() - make_interval(days => 90);

ALTER TABLE session_events ENABLE TRIGGER session_events_no_mutation;

COMMIT;
