# Checkpoint B review packet (Phase 5 search)

Generated 2026-10-04T00:26:02Z by scripts/search/checkpoint_b_packet.py (read-only, rerunnable). Full exports under data/phase5-search/checkpoint-b/ (gitignored). No model output is quoted verbatim below: answer text, titles, excerpts and summaries are paraphrased or counted; URLs are operational facts from the logs.

## 1. Sessions in scope

Every live session from the Phase 5 runs and the P3-L-13 runs on 2026-10-03. Session ids come from the application database (authoritative: raw trails under data/phase3-live/ are overwritten per scenario key, so only the last run survives there) corroborated against the raw-trail ids and mapped onto the preserved summaries by timestamp order.

| Session (suffix) | Run | Scenario | Stop | Outcome |
|---|---|---|---|---|
| 8668f0 | live-summary-p3l13-attempt1.json | live-ask-resume-p3l13 | agent_token_budget_exhausted | stopped: token budget exhausted (no answer) |
| b0b52b | live-summary-phase5-step1.json | live-search-missing-dish | agent_needs_user_input | asked the user (awaiting input) |
| 0e4b06 | live-summary-p3l13-attempt2.json | live-ask-resume-p3l13 | agent_needs_user_input | asked the user (awaiting input) |
| 27eb83 | live-summary-phase5-step2.json | live-search-missing-dish | agent_needs_user_input | asked the user (awaiting input) |
| 67127c | live-summary-phase5-step3.json | live-search-missing-dish | agent_needs_user_input | asked the user (awaiting input) |
| ec7a89 | live-summary-phase5-step4.json | live-search-missing-dish | agent_no_progress | stopped: no progress (no answer) |
| eb6463 | live-summary-phase5-step5.json | live-search-missing-dish | agent_sufficient_evidence | web_answer accepted — one demonstrated discovery answer after fixes, not a reliability result |

Mapping notes:
- ses-live-live-search--a1-b0b52b: id absent from data/phase3-live (trail overwritten by a later run; mapped from DB + summary)
- ses-live-live-search--a1-27eb83: id absent from data/phase3-live (trail overwritten by a later run; mapped from DB + summary)
- ses-live-live-search--a1-67127c: id absent from data/phase3-live (trail overwritten by a later run; mapped from DB + summary)
- ses-live-live-search--a1-ec7a89: id absent from data/phase3-live (trail overwritten by a later run; mapped from DB + summary)
- ses-live-live-ask-res-a1-8668f0: id absent from data/phase3-live (trail overwritten by a later run; mapped from DB + summary)

## 2. Search log completeness

Required per dispatched search: search_slot_claimed, search_requested, search_results_retrieved, evidence_evaluated, search_outcome, search_operations, and the search_web tool_call (correlated by call_id). Costs are reconciled ledger figures from the spend histories, including the appended correction. The 7 live searches predate the citation provenance check and did not store action_sources, so their URL provenance cannot be verified after the fact (this does not mean the live links were invented); their audit fields show 'not recorded (pre-fix)'.

### 8668f0 (live-summary-p3l13-attempt1.json, live-ask-resume-p3l13)
- no searches dispatched in this session.

### b0b52b (live-summary-phase5-step1.json, live-search-missing-dish)
- call call_XGUFFdD: search_slot_claimed: present, search_requested: present, search_results_retrieved: present, evidence_evaluated: present, search_outcome: present, search_operations: present, tool_call: present.
  latency 7185.08 ms; tokens in/out 8846/508; cost $0.01114 (reconciled); slots 1/3; sources 3 {'unclassified/kept': 3}; provenance: not recorded (pre-fix).

### 0e4b06 (live-summary-p3l13-attempt2.json, live-ask-resume-p3l13)
- no searches dispatched in this session.

### 27eb83 (live-summary-phase5-step2.json, live-search-missing-dish)
- call call_eXP8DQo: search_slot_claimed: present, search_requested: present, search_results_retrieved: present, evidence_evaluated: present, search_outcome: present, search_operations: present, tool_call: present.
  latency 7424.14 ms; tokens in/out 8981/550; cost $0.01117 (reconciled); slots 1/3; sources 3 {'unclassified/kept': 3}; provenance: not recorded (pre-fix).
- call call_hwHItju: search_slot_claimed: present, search_requested: present, search_results_retrieved: present, evidence_evaluated: present, search_outcome: present, search_operations: present, tool_call: present.
  latency 4102.04 ms; tokens in/out 8876/279; cost $0.01103 (reconciled); slots 2/3; sources 1 {'unclassified/kept': 1}; provenance: not recorded (pre-fix).
- call call_eej9S9w: search_slot_claimed: present, search_requested: present, search_results_retrieved: present, evidence_evaluated: present, search_outcome: present, search_operations: present, tool_call: present.
  latency 5691.89 ms; tokens in/out 8848/432; cost $0.01110 (reconciled); slots 3/3; sources 3 {'unclassified/kept': 3}; provenance: not recorded (pre-fix).

### 67127c (live-summary-phase5-step3.json, live-search-missing-dish)
- call call_BO0g0PO: search_slot_claimed: present, search_requested: present, search_results_retrieved: MISSING, evidence_evaluated: MISSING, search_outcome: MISSING, search_operations: MISSING, tool_call: present.
  latency None ms; tokens in/out None/None; cost $0.02500 (kept-ambiguous); slots 1/1; sources 0 {}; provenance: not recorded (pre-fix). NOTE: search-1-correction: kept-ambiguous after timeout.
  MISSING EVENTS: search_results_retrieved, evidence_evaluated, search_outcome, search_operations.
- call call_v7r6KMm: REFUSED search_budget_exhausted (tool_call only, no slot, no provider call).

### ec7a89 (live-summary-phase5-step4.json, live-search-missing-dish)
- call call_cWjnQ3I: search_slot_claimed: present, search_requested: present, search_results_retrieved: present, evidence_evaluated: present, search_outcome: present, search_operations: present, tool_call: present.
  latency 6428.66 ms; tokens in/out 9032/481; cost $0.01114 (reconciled); slots 1/1; sources 3 {'unclassified/kept': 3}; provenance: not recorded (pre-fix).
- call call_Kk0m2mu: REFUSED search_budget_exhausted (tool_call only, no slot, no provider call).
- call call_0ZzHNRu: REFUSED search_budget_exhausted (tool_call only, no slot, no provider call).

### eb6463 (live-summary-phase5-step5.json, live-search-missing-dish)
- call call_iGUtOxj: search_slot_claimed: present, search_requested: present, search_results_retrieved: present, evidence_evaluated: present, search_outcome: present, search_operations: present, tool_call: present.
  latency 8454.18 ms; tokens in/out 8949/473; cost $0.01113 (reconciled); slots 1/1; sources 3 {'unclassified/kept': 3}; provenance: not recorded (pre-fix).

Completeness findings: the timed-out search (step 3) holds only slot + request + tool_call — results, evaluation, outcome and operations were never recorded because the tool-level timeout fired outside the implementation. That gap is historical: step 3 ran under the old 10 s tool / 10 s provider timeouts, and it stays shown as is. Under the new defaults the provider's own 20 s timeout fires before the 30 s tool timeout and is logged as outcome 'error'; search_web_impl now also records outcome 'cancelled' when tool-level cancellation interrupts the provider call. Every other dispatched search has the full chain; the three refusals (one step-3 retry after the timeout, two step-4) are tool_call-only by design.

## 3. Privacy check (heuristics)

Method: the email / phone / street-address / "my <Name>" patterns from search/minimize.py run as detectors over every stored payload for these sessions (event payloads incl. recorded tool args), the packet exports, and the raw files under data/phase3-live/ and data/phase5-live/. Limits, stated honestly: regexes miss paraphrases, non-English text and novel formats; a zero count is not a guarantee. Queries were synthetic, so zero PII hits are expected.

- detector hits: 4 across 1 field report(s). Any hit needs a manual disposition: the detectors also match digit strings such as revision ids, so a count alone is not a PII verdict.
  - phone: 4 in file:data/phase3-live/live-technique-question.json :: (whole file)
- Disposition (verified 2026-10-03): all 4 phone-pattern matches sit in the technique-source attribution metadata of data/phase3-live/live-technique-question.json (whole-file scan). Each match was extracted with its surrounding context and all four are the same repeated 10-digit Wikipedia revision identifier, not a phone number. No email, address or name hits anywhere.
- URLs checked: 14; query/fragment issues: 0.
- recorded args are minimized too: tool_call.args is stored only because the live runner sets record_tool_args=True (the ToolContext default is False, digest only). Every live search_web tool_call row carries args as minimize_tool_args JSON — the same scrubber as minimized_query — verified from the stored rows. The default application path stores args_digest (truncated sha256 hex) only, never free text.
- stored free text that is NOT minimized (owner judges risk):
  - user_message text; agent_question question_text, options and note;
  - agent_finished note; agent_validation_reject errors;
  - result_facts titles in search_web tool_call events;
  - the raw trails under data/phase3-live/ (whole files).
- retention coverage after the fix: the purge procedure deletes expired search_* events and search_web tool_call events (minimized args, urls, result_facts titles included). Everything above the procedure line persists as session data: user_message, agent_question, agent_finished, agent_validation_reject, other agent and non-search tool events, and the raw-trail files (owner file-purge only).
- free-text inventory, grouped (full per-field list in privacy.json):
  | source | fields | max chars | minimized |
  |---|---|---|---|
  | db event agent_finished | 1 | 106 | no |
  | db event agent_question | 16 | 250 | no |
  | db event agent_step | 7 | 23 | no |
  | db event agent_validation_reject | 3 | 77 | no |
  | db event evidence_evaluated | 12 | 102 | yes |
  | db event search_requested | 5 | 73 | yes |
  | db event search_results_retrieved | 12 | 102 | yes |
  | db event tool_call | 7 | 89 | yes |
  | db event tool_call | 14 | 64 | no |
  | db event user_message | 7 | 87 | no |
  | packet exports | 98 | 30974 | mixed |
  | raw file data/phase3-live/live-ask-resume-p3l13.json | 1 | 11445 | no |
  | raw file data/phase3-live/live-chicken-dinner.json | 1 | 21262 | no |
  | raw file data/phase3-live/live-direct-lentil.json | 1 | 15815 | no |
  | raw file data/phase3-live/live-empty-retrieval.json | 1 | 5561 | no |
  | raw file data/phase3-live/live-epicure-unavailable.json | 1 | 13537 | no |
  | raw file data/phase3-live/live-roast-pairing.json | 1 | 15963 | no |
  | raw file data/phase3-live/live-search-missing-dish.json | 1 | 9881 | no |
  | raw file data/phase3-live/live-technique-question.json | 1 | 6655 | no |
  | raw file data/phase3-live/live-vegetarian-conflict.json | 1 | 17251 | no |
  | raw file data/phase3-live/live-yogurt-ask.json | 1 | 29568 | no |
  | raw file data/phase3-live/spend-history.json | 1 | 50894 | no |
  | raw file data/phase5-live/spend-history.json | 1 | 6422 | no |

## 4. Gap candidates (unreviewed)

Derived read-only via scripts/search/gap_export.py over the exported events (nothing imported; review_status unreviewed; triggers are candidates, never causes).

### Phase 5 + P3-L-13 (2026-10-03): 5 candidate(s)
- missing_recipe: 4 (sessions 27eb83, b0b52b, eb6463, ec7a89).
  signal: search_recipes result_count==0 then search_web ok in same session.
  alternative: zero results may be missing data or poor retrieval, not a corpus gap.
  usefulness: shows which dishes users ask for but the corpus lacks; needs review since weak retrieval mimics a gap.
- retrieval_miss: 1 (sessions 8668f0).
  signal: zero-result search then get_recipe hit on a manual id.
  alternative: zero results may be missing data or poor retrieval.
  usefulness: shows queries the corpus could answer but retrieval missed; useful for retrieval tuning, not corpus growth.

### Phase 3 live (2026-09-30): 25 candidate(s)
- missing_ingredient_alias: 12 (sessions 124474, 21c90f, 3d8e67, 45c570, 5c3020, 8b1ea8, 909058, a413f5, b2c909, e068b8, e59a9d, ebbe66).
  signal: epicure tool_invalid_arguments (unknown ingredient) in session.
  alternative: an unknown Epicure ingredient may be vocabulary coverage, not a missing alias.
  usefulness: shows vocabulary users employ that Epicure rejects; needs review since coverage gaps mimic missing aliases.
- retrieval_miss: 13 (sessions 124474, 45d55b, 57bbac, 5c3020, 60492d, 7e0505, 909058, 91cc04, ad93cc, b2c909, e068b8, e59a9d, ebbe66).
  signal: zero-result search then get_recipe hit on a manual id.
  alternative: zero results may be missing data or poor retrieval.
  usefulness: shows queries the corpus could answer but retrieval missed; useful for retrieval tuning, not corpus growth.

## 5. Retention and access

- stored where: session rows + session_events in the application database (append-only); raw trails data/phase3-live/*.json; spend histories data/phase5-live/spend-history.json and data/phase3-live/spend-history.json; this packet's exports under data/phase5-search/checkpoint-b/ (all gitignored).
- expiry: 90 days for search events, 30 days for process logs (Settings SEARCH_EVENT_RETENTION_DAYS / PROCESS_LOG_RETENTION_DAYS).
- purge procedure (fixed for this packet, still NOT run): one transaction that DISABLEs the append-only trigger, DELETEs expired rows, re-ENABLEs the trigger and COMMITs — ALTER TABLE is transactional, so any failure rolls back with the trigger still enabled (brief ACCESS EXCLUSIVE lock, table-owner privilege, tested on a disposable DB). Scope: the six search_* event types plus search_web tool_call events (recorded args, hosts, titles). user_message, agent_question and all other agent events persist as session data. Backup first, 90-day default (single value in the SQL file).
- purge commands below are shown dry-run only; nothing was deleted. The SQL procedure was NOT run.
```
expired files: 0
dry run: pass --apply to delete (backup copies first)
--- purge_search_events.sql (verbatim; run by the owner with a backup) ---
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
```
purge_search_events.sql (procedure, not run):
```sql
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
```
- readers: the owner only, through review exports that stay out of git. Nothing is stored for real users; the search toggle stays off by default.

## 6. Spend (recomputed from the history files)

- Phase 5 pool, budget accounting (not invoiced spend): reconciled $0.07649 (searches $0.06671 + model/embedding $0.00978) + timeout reservation $0.02500 = $0.1015 of $0.13 (7 dispatched searches, 8 ledger entries).
  - 2026-10-03T19:43:02Z search-1: $0.01114 (reconciled)
  - 2026-10-03T21:09:23Z search-1: $0.01117 (reconciled)
  - 2026-10-03T21:09:23Z search-2: $0.01103 (reconciled)
  - 2026-10-03T21:09:23Z search-3: $0.01110 (reconciled)
  - 2026-10-03T21:41:49Z search-1: $0.00000 (reserved) (superseded by the appended correction)
  - 2026-10-03T21:41:49Z search-1-correction: $0.02500 (kept-ambiguous)
  - 2026-10-03T22:18:34Z search-1: $0.01114 (reconciled)
  - 2026-10-03T22:34:04Z search-1: $0.01113 (reconciled)
- Phase 3 pool: $0.1304 of $0.15.
- Milestone 3 total: $0.2319.

## 7. Checkpoint B questions for the owner (unanswered)

Owner decisions: docs/phase5-owner-decisions.md, section "Checkpoint B: owner decisions (2026-10-03)". The questions below restate the packet context for the owner; they are not answered here.


1. Approve the provider (OpenAI hosted web_search via the search_web wrapper)
   context: 6 of 7 searches reconciled at about $0.011 against the $0.025 estimate; one timed out and is kept-ambiguous at $0.025. The wrapper enforces permission, slot limits, minimization and the 30 s tool / 20 s provider timeouts; the agent sees only the checked summary plus up to 5 sources. Provenance caveat: the 7 live searches predate the citation check and did not store action_sources, so their URL provenance cannot be verified after the fact.
   recommendation: approve the wrapper as the Phase 5 provider path.

2. The per-session limit (3)
   context: step 2 ran three searches although the owner had authorized 2 for that step: the limit flags were checked only in preflight, not enforced in-run (recorded in docs/phase5-owner-decisions.md). Fixed by SearchRunLimits: steps 3-5 ran one slot each with refusals working (one step-3 retry and two step-4 attempts refused with search_budget_exhausted).
   recommendation: keep 3 in code with 1-2 for live checks.

3. The Phase 5 ceiling as finally used ($0.13)
   context: recomputed pool $0.1015 of $0.13; the campaign is complete at 7 of 7 searches with no further searches planned.
   recommendation: accept $0.13 as the final Phase 5 ceiling.

4. The logs: useful and privacy-acceptable?
   context: every dispatched search is traceable call by call (slot, request, results, evaluation, outcome, operations); the timed-out search left no outcome/operations events. Privacy scan over stored payloads, exports and raw files is reported above; all queries were synthetic and the detectors are narrow heuristics.
   recommendation: accept the logs as useful; accept privacy for development data and re-review before any real-user storage.

5. The gap queue: useful? Keep events plus export, or plan a table later?
   context: candidates are derived views with provenance and alternatives, all unreviewed; nothing is imported automatically.
   recommendation: keep events plus export; revisit a table only if Phase 6/7 needs a review interface.

6. Anything to change before Phase 6 (the minimal UI)?
   context: under the new defaults the provider's own 20 s timeout fires before the 30 s tool timeout and is logged as outcome 'error'; search_web_impl now also records outcome 'cancelled' when tool-level cancellation interrupts the provider call. The step-3 gap is historical under the old 10 s/10 s settings.
   recommendation: no changes before Phase 6.
