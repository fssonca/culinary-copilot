-- Search retention purge (Phase 5, part 2, owner decision 8).
-- Owner-run only, never automatic. Tested on disposable databases only.
-- The append-only trigger (005) rejects plain rewrites; deletes run
-- inside this procedure transaction with a backup first.
--
-- Defaults: 90 days for session search events, 30 days for process
-- logs (Settings SEARCH_EVENT_RETENTION_DAYS /
-- PROCESS_LOG_RETENTION_DAYS). Process logs are files (see
-- scripts/search/purge_search.py); this procedure covers DB events.
--
-- 1) Back up first, e.g.:
-- COPY (SELECT * FROM session_events WHERE event_type IN
--   ('search_slot_claimed','search_requested','search_results_retrieved',
--    'evidence_evaluated','search_outcome','search_operations'))
--   TO '/backup/search-events-YYYYMMDD.csv' CSV HEADER;
--
-- 2) Run the delete (adjust :retention_days, default 90):

BEGIN;

DELETE FROM session_events
 WHERE event_type IN (
   'search_slot_claimed',
   'search_requested',
   'search_results_retrieved',
   'evidence_evaluated',
   'search_outcome',
   'search_operations'
 )
 AND created_at < now() - make_interval(days => 90);

COMMIT;
