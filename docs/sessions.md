# Agent sessions and cooking phases (Milestone 3, Phase 1 — implemented)

Backend-only. Sessions persist agent state in Postgres (migration `005`);
the in-memory clarification store stays for the existing clarification,
retrieval and recommendation endpoints. Later phases build the typed tool
layer and bounded loop on top of this model; no agent loop exists yet.

## What lives where

| State | Location | Lifetime |
|---|---|---|
| Clarification requests/groups | Process-local `InMemoryClarificationStore` (512-request FIFO) | Lost on restart; single worker |
| Agent sessions + event log | Postgres `sessions` / `session_events` (migration `005`) | Durable across restarts |

Sessions link to clarification by ID (`clarification_request_id`,
`clarification_group_id`) rather than copying clarification state. The
server re-reads clarification state from its authoritative store when it
needs it; the session row only remembers which request/group it refers to.

Stored on the session row: constraints, confirmed answers, unresolved
questions, Epicure outcome or skip reason, suggestions, selected dish,
cooking plan, current phase, evidence/source references,
internet-search permission (default off), remaining tool budget
(`tool_calls_remaining` / `steps_remaining`, server-set from
`SESSION_MAX_TOOL_CALLS` / `SESSION_MAX_STEPS`: defaults 12 / 8 —
Checkpoint 0 budgets: MAX_STEPS 8, 12 tool calls per session).

## Phases and allowed transitions

Phases are data in `domain/sessions.py::ALLOWED_TRANSITIONS`, not control
flow. A same-phase update is always allowed (no transition). Any other
move not listed is rejected with the stable reason
`invalid_phase_transition` (`next_action: change_request`).

| From | To (allowed) |
|---|---|
| `discover` | `clarify`, `research`, `recommend` |
| `clarify` | `research`, `recommend`, `discover` |
| `research` | `recommend`, `clarify` |
| `recommend` | `select`, `plan` (direct recipe/technique requests may skip `select`), `clarify` (hard-constraint rejection returns to change constraints) |
| `select` | `plan`, `recommend` |
| `plan` | `cook`, `select` |
| `cook` | `plate`, `plan` |
| `plate` | _(terminal; no outgoing moves)_ |

## Endpoints

Create is server-set: every session starts in `discover` with budgets
from Settings. Client-supplied `current_phase`, `tool_calls_remaining`
or `steps_remaining` are rejected with 422 (FastAPI `extra="forbid"`).

```sh
# Create (phase discover, budgets from Settings; permission off).
curl -X POST http://localhost:8000/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"clarification_request_id": "req-abc", "clarification_group_id": "grp-abc"}'
# A body carrying current_phase / tool_calls_remaining / steps_remaining
# gets 422.

# Read (session + append-only events).
curl http://localhost:8000/api/v1/sessions/<ses-id>

# Internet-search permission (compare-and-set on revision; off by default).
curl -X POST http://localhost:8000/api/v1/sessions/<ses-id>/permission \
  -H 'Content-Type: application/json' \
  -d '{"revision": 1, "allowed": true}'
```

Error behavior (every body with a `reason` also carries `next_action`
via `domain/recommendations.py::next_action_for`):

| Situation | Status | `reason` / `next_action` |
|---|---|---|
| Unknown session | 404 | `unknown_session` / `change_request` |
| Stale `revision` (another writer won) | 409 | `stale_revision` / `refetch_and_retry` |
| Illegal phase move | 422 | `invalid_phase_transition` / `change_request` |
| Malformed payload (incl. client budgets/phase, merge overflow past 200) | 422 | `malformed` / `change_request` |
| Session tables missing (`UndefinedTable` / `to_regclass` NULL; 005 pending) | 503 | `session_store_not_migrated` / `contact_operator` |
| Other session DB errors | 503 | `session_unavailable` / `retry` |

Concurrency mirrors clarification: reads return detached copies; updates
commit only when the stored revision still matches the caller's snapshot.
Confirmed answers are append-only at the store layer
(`merge_confirmed_answers`): an update that omits a stored answer keeps it;
an explicit correction with the same question key replaces it; a union past
the 200-answer cap raises (422 `malformed`) instead of dropping.

## Migration 005

`src/culinary_copilot/recipes/migrations/005_sessions.sql` (plain
Postgres, no vector extension requirement — applies on stock `postgres:17`
and on the pgvector image). Additive only; `001`–`004` stay byte-for-byte
unchanged.

- `sessions`: revision, current phase, clarification IDs, constraints,
  confirmed answers, unresolved questions, Epicure outcome/skip reason,
  suggestions, selected dish, cooking plan, evidence, permission default
  off, tool/step budgets, timestamps.
- `session_events`: `(session_id, seq)` log with a `BEFORE UPDATE OR
  DELETE` trigger rejecting rewrites (append-only; application code only
  INSERTs/SELECTs).

Rollback (manual; rehearsed on a disposable database, never on the
application database):

```sql
DROP TRIGGER IF EXISTS session_events_no_mutation ON session_events;
DROP FUNCTION IF EXISTS prevent_session_events_mutation();
DROP TABLE IF EXISTS session_events;
DROP TABLE IF EXISTS sessions;
DELETE FROM recipe_schema_migrations WHERE version = '005';
```

## Application migration package (applied 2026-09-28 with owner approval)

**Applied to `localhost:5432/culinary_copilot` on 2026-09-28** after owner
go-ahead. Recorded outcome:
- pre-checks: ledger `001`–`004`, `16033` recipes, `443` quarantine, no
  `sessions` table;
- backup `data/session-migration-backup-005.sql` (511 MB, sha256
  `7dacf828…`, not in Git), restored into `culinary_check_restore_005` with
  `16033` / `443` and ledger `001`–`004`;
- migrate: `applied=['005'] skipped=0`;
- post-checks: ledger `001`–`005` with unchanged checksums, `16033` / `443`,
  `sessions` and `session_events` present; API smoke created a session
  (revision 1, `discover`, search off, budgets 12/8) and read it back with a
  `created` event (one smoke-test session row remains).

The steps below are kept as the record of what was run and as the template
for re-applying after a rollback. Earlier rehearsals used disposable
databases (`cc_disposable_check` on stock `postgres:17`;
`culinary_test_sessions` on the local pgvector container).

1. Target identity checks (read-only; abort unless all match):
   ```sh
   uv run python -c "
   from sqlalchemy import create_engine, text
   from culinary_copilot.config import Settings
   eng = create_engine(Settings().database_url.get_secret_value())
   with eng.connect() as c:
       print('ledger:', [r[0] for r in c.execute(text('SELECT version FROM recipe_schema_migrations ORDER BY version'))])
       print('recipes:', c.execute(text('SELECT count(*) FROM recipes')).scalar())
       print('quarantine:', c.execute(text('SELECT count(*) FROM recipe_quarantine')).scalar())
       print('sessions table:', c.execute(text(\"SELECT to_regclass('sessions')\")).scalar())
   "
   # Expected pre-migration: ledger 001-004, recipes 16033, quarantine 443,
   # sessions table None.
   ```

2. Backup + restore verification:
   ```sh
   docker compose exec -T db pg_dump -U copilot -d culinary_copilot \
     > data/session-migration-backup-005.sql
   # Verify: new disposable restore target whose name contains check/test/disposable.
   docker exec culinary-copilot-db-1 psql -U copilot -d postgres \
     -c 'DROP DATABASE IF EXISTS "culinary_check_restore_005";'
   docker exec culinary-copilot-db-1 psql -U copilot -d postgres \
     -c 'CREATE DATABASE "culinary_check_restore_005";'
   cat data/session-migration-backup-005.sql | \
     docker exec -i culinary-copilot-db-1 psql -U copilot -d culinary_check_restore_005
   docker exec culinary-copilot-db-1 psql -U copilot -d culinary_check_restore_005 \
     -c 'SELECT count(*) FROM recipes; SELECT count(*) FROM recipe_quarantine;'
   # Expected: same 16033 / 443 counts on the restore target before proceeding.
   ```

3. Exact migrate command (refuses unless target matches):
   ```sh
   uv run python scripts/migrate.py \
     --expect-db-name culinary_copilot --expect-db-host localhost
   # Uses DATABASE_URL from .env (localhost:5432/culinary_copilot).
   # Expected: applied=['005'], skipped=0 on the pgvector image.
   ```

4. Post-migration checks (read-only; abort/roll back on mismatch):
   - `SELECT version FROM recipe_schema_migrations` is `001,002,003,004,005`;
   - `SELECT count(*) FROM recipes` still `16033`;
   - `SELECT count(*) FROM recipe_quarantine` still `443`;
   - `001`–`004` checksums unchanged (migration runner fails closed on any
     byte change; `git status` shows no modification to `001`–`004`);
   - `SELECT to_regclass('sessions'), to_regclass('session_events')` both present;
   - `POST /api/v1/sessions` creates (revision 1, permission off),
     `GET /api/v1/sessions/<id>` reads back.

5. Rollback steps (only with owner approval; backup above must exist):
   ```sql
   DROP TRIGGER IF EXISTS session_events_no_mutation ON session_events;
   DROP FUNCTION IF EXISTS prevent_session_events_mutation();
   DROP TABLE IF EXISTS session_events;
   DROP TABLE IF EXISTS sessions;
   DELETE FROM recipe_schema_migrations WHERE version = '005';
   ```
   Then re-verify recipe/quarantine counts (`16033` / `443`) and that
   `POST /api/v1/sessions` returns `503 session_store_not_migrated` until `005`
   is re-applied.

## Migrating the existing endpoints off the in-memory store (later; not done now)

The existing clarification, retrieval and recommendation endpoints still
use `InMemoryClarificationStore` and are unchanged. A future migration
would, in order:

1. Dual-write clarification state to Postgres (new `clarification_*`
   tables or JSONB columns) while keeping the in-memory store
   authoritative; verify parity on reads.
2. Flip reads to Postgres with the same snapshot/CAS semantics (deep
   copies, `request_revision`/`group_revision` compare-and-set, 409 on
   stale, replan supersede logic unchanged).
3. Point retrieval/recommendation readiness checks at the Postgres-backed
   clarification state (same recompute-from-authoritative-state rule).
4. Retire the in-memory store once the 512-entry cap, restart loss and
   single-worker scope are no longer load-bearing in any test or runbook.
5. Backfill nothing: historical in-memory groups are ephemeral by design;
   only sessions (which already reference clarification IDs) persist.

No step above is implemented in Phase 1. In particular, `POST
/api/v1/clarification/groups`, `/answers`, `/replan`, `POST
/api/v1/retrieval/search` and `POST /api/v1/recommendations` still take
their revisions from, and commit to, process memory.

## Planned vs implemented

- Implemented (Phase 1): `005` schema, phase table + validation,
  Postgres CAS store with merge + event log, create/read/permission
  endpoints, disposable-DB tests + rehearsal.
- Planned (later phases, not implemented): typed tool layer, bounded
  agent loop enforcing `tool_calls_remaining`/`steps_remaining`,
  `search_recipes` mode argument, technique corpus, permission-gated web
  search execution, UI, scenario evals. Budgets above are stored, not yet
  enforced.
