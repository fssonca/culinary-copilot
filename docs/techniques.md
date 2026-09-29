# Technique corpus (Milestone 3, Phase 4)

Owner-approved corpus of cooking-technique documents backing
`search_techniques`. Technique references are a **separate evidence
type** from recipe identities (`dataset_id`, `source_id`): they may
support a technique claim in a plan/cook step, never dish identity,
quantities, options, or recipe sources.

Status (2026-09-28): **part 2 implemented; app apply and paid
embedding pending owner go-ahead.** Full-text path works on both
images; the vector path is implemented and rehearsed with fakes only.

## Corpus

- Allowlist: `evals/technique_corpus/approved_sources.json` (40 owner-
  approved ids, §7; committed, no document text). The fetcher
  (`scripts/techniques/fetch.py`) reads only that file and refuses any
  other id/URL (tested).
- Fetch: Wikimedia Action API `action=parse` (serial, `maxlag`,
  `User-Agent: culinary-copilot-technique-corpus/0.1
  (https://github.com/fssonca/culinary-copilot)`), direct HTTPS for
  FDA/FSIS after a `robots.txt` check. Retries on transient errors
  only; blocks are never worked around.
- Outcome: **34 ingested, 6 dropped** (manifest
  `evals/technique_corpus/manifest.json` with per-doc URL, publisher,
  licence, attribution text, retrieval date, revision id, sha256 of raw
  + normalized text, words, bytes, status/reason):
  - too-short (<150 words): `tech-beans-25` (138), `tech-measure-12`
    (136), `tech-caramel-30` (75), `tech-blind-28` (42);
  - blocked (HTTP 403, no workaround): `tech-fsis-temp-34`,
    `tech-fsis-leftover-36` (food-safety cover remains via the two FDA
    pages + `tech-freeze-R6`).
- Totals: **34,890 words, 213,301 bytes, 253 chunks, ~51,868
  estimated tokens** (chars/4; embedding reservation uses the
  bytes+8 upper bound, see below).
- Storage: raw + normalized text under `data/technique-corpus/`
  (git-ignored; verified with `git check-ignore` before every write).
  No document text is committed anywhere.
- Normalization (normalizer v2, `scripts/techniques/fetch.py`) drops
  References / See also / External links / Further reading / Notes,
  navigation, and footers; keeps `##` section headings for chunking.
  Table rule: a `<table>` is kept iff its own markup contains at
  least one `<th>` — kept tables render one row per line with
  header/cells joined by ` | `, the `<caption>` kept as a paragraph,
  the preceding heading staying the section. Th-less tables are
  discarded as layout/navigation (observed: Wikibooks shelf navbars),
  as are tables inside skipped chrome (nav, aside, footer, infobox,
  navbox, metadata). Degree markup (`<sup>o</sup>`, `&deg;`, glued
  `°`) normalizes to spaced `°` (`165 °F`). Renormalized from the
  saved raw files with no refetch: only two docs changed —
  `tech-fda-safe-32` 845 → 862 words (temperature-table caption +
  degree spacing; poultry 165 °F, ground meat 160 °F, whole cuts
  145 °F + 3-minute rest, leftovers 165 °F now present) and
  `tech-bread-27` 574 → 558 (nav table dropped); plus `tech-freeze-R6`
  379 → 382 (degree spacing). Retrieval dates and raw hashes
  unchanged. The 6 dropped docs stay dropped (no raw files were ever
  saved for them, so table text cannot promote them — verified by
  construction, not re-admitted).

## Schema (migrations 006 + 007)

- `006_technique_corpus.sql` (plain PostgreSQL, applies on **both**
  images): `technique_documents` (provenance columns per §4a) +
  `technique_chunks` (`doc_id`, `chunk_id`, `section`, `chunk_text`,
  `search_vector` tsvector with GIN index; the loader fills the
  vector with `to_tsvector('english', ...)`).
- `007_technique_embeddings.sql` (`requires-extension: vector`,
  pgvector only): `technique_embeddings` reusing the 004 column
  discipline (dimension-agnostic `vector`, dimension + `vector_dims`
  CHECKs, cascading FK to chunks, full identity: model, dimension,
  technique renderer/chunking versions, embedded-text hash). The run
  ledger reuses `embedding_runs` (technique runs use a `tech-` prefix).
- `001`–`005` byte-for-byte unchanged. Stock `postgres:17` skips 004
  and 007 with a reason and runs full-text on 006 alone (rehearsed).

## Chunking

`embeddings/technique_rendering.py` (independent of
`embeddings/rendering.py`): `TECHNIQUE_RENDER_VERSION = "2"`
(v2: data-table rows are chunk text), `TECHNIQUE_CHUNK_VERSION =
"1"`. Section-aware: never crosses a `##` boundary; target ~400
tokens, hard max 800 (chars/4; join separators charged). Real
corpus: 253 chunks, max 785 tokens.

## Usage reporting (estimates vs provider-reported usage)

Embedding CLIs (`scripts/techniques/embed_techniques.py`,
`scripts/embeddings/embed.py`, `scripts/techniques/eval_baseline.py
--live`) work in two numbers:

- `reserved` / `estimated`: the pre-run byte estimate (UTF-8 bytes +
  8 per input, × retries+1). Used for ceiling checks before any call.
- `used`: provider-REPORTED `usage.prompt_tokens`, summed per
  request. This is the billed figure, stored in `embedding_runs` and
  the run ledger (`used_tokens_total`, cumulative) and printed with
  its cost. The estimate lives on separately (`reserved_tokens`,
  `estimated_tokens_total`).

Runs before this fix recorded the estimate as usage; their ledgers
and `embedding_runs` rows are NOT rewritten. That includes the
technique app run below: its recorded estimate is 421,108 tokens
(210,554 per attempt, retry ×2). Real billed usage is unknown —
roughly ~52k tokens expected from the chars/4 chunk estimate
(51,868) — and the owner can confirm it from the OpenAI usage page.

## Tool

`search_techniques(query, mode?, limit?)` (Phase 2 schema + optional
`mode`; omitted mode follows `TECHNIQUE_RETRIEVAL_MODE`, default
`fulltext`). Send short keyword queries (2–5 content terms; the tool
and argument descriptions say so). Full-text mirrors the recipe
contract (`recipes/repository.py`: `plainto_tsquery` AND per chunk,
`ts_rank_cd` ordering): every term must occur in one chunk first
(`match="all"`). When the all-terms match returns nothing, the same
lexemes retry with OR semantics (`match="any"`) so a long
natural-language query returns the best partial matches instead of an
empty result. `match` is recorded in the tool result, the `tool_call`
event, the agent history summary, and the eval rows. The fallback is
deliberately untuned: no per-query or per-case special cases.
Vector cutoff pinned `0.66` (mirrors `search_recipes`, but that value
is recipe-calibrated and UNCALIBRATED for technique chunks — the
vector eval reports both figures so the cutoff can be set from data).
Missing 006 tables → `tool_not_configured` / `contact_operator`
(never `retry` for a missing table); vector without embeddings,
pgvector, or 007 rows → typed `unavailable` with `contact_operator`
and no silent fallback; `mode_ran` returned and logged. Every hit:
`doc_id`, `chunk_id`, `section`, `title`, `url`, `licence`,
`licence_url`, `attribution_text`, bounded excerpt (600 chars).

Agent use: send short keyword queries (2–5 content terms; full-text is
`plainto_tsquery` AND per chunk). Every excerpt returned by the tool,
the SSE stream, or session evidence carries `attribution_text` and
`licence_url` (owner share-alike condition; tests fail otherwise).

## Validation

`agent/validate.py::validate_technique_refs`: each ref needs
`doc_id` + `chunk_id`, must resolve in the corpus, and must have been
returned by a `search_techniques` call in the same session (tracked
per run in `agent/loop.py`); refs with `dataset_id`/`source_id` are
rejected. Options carrying technique keys are rejected
(`TECHNIQUE_OPTION_KEYS`). Validated refs land in session `evidence`
(`technique_refs` with attribution). No new stop reasons were needed;
`tests/test_next_action.py` scans the new modules.

## Evaluation (`evals/technique_retrieval/`)

- `cases.json`: 16 AI-drafted cases (6 paraphrase ≥ 1/3), doc-level
  labels from manifest + document text, frozen with in-file
  `freeze_sha256` (`762d966c…dfa2a5`; a test verifies the hash).
- Honesty record: an exploratory full-text run was made with the v1
  queries BEFORE any freeze, contrary to the freeze-before-any-run
  instruction. v1 queries were over-long natural-language sentences;
  the then AND-only matcher returned mostly empty results (HitRate@5
  0.125, MRR 0.219 — and the runner's hit rule then wrongly required
  all relevant docs instead of any). v1 → v2 changed QUERIES ONLY
  (shortened to 2–4 keyword queries); the relevance labels are
  identical (the revision touched only the query fields). The hit rule
  was corrected to any-relevant before the official run. The honesty
  note sits inside the frozen fields, so the freeze hash was
  recomputed for the note addition; queries and labels are untouched
  by it.
- Full-text baselines (`scripts/techniques/eval_baseline.py`,
  `baseline_fulltext.json`, same frozen cases, reuses
  `scripts/retrieval_eval/metrics.py::reciprocal_rank`):
  - before (AND-only): HitRate@5 0.625, MRR 0.594;
  - after (all-terms first, OR fallback with `match` recorded):
    HitRate@5 0.812, MRR 0.781;
  - after table fix (renormalized corpus, same frozen cases):
    **HitRate@5 0.812, MRR 0.781 — per-case identical.** The figures
    are safe: `tq-04` (`chicken internal temperature`) still misses
    because the FDA doc says "poultry", never "chicken" — lexical,
    not a table loss (`165 poultry` now hits `tech-fda-safe-32`
    chunk 6 top-1 with `match=all`, verified by probe). `tq-04`
    joins `tq-15` as a vector-run probe.
- Owner spot-checks: food-safety cases (`tq-04`, `tq-05`, `tq-15`);
  sear/sauté/braise distinctions (`tq-01`, `tq-02` — top hit is
  roast-03, `tq-07`); short-Wikibooks top hits (`tq-12`
  rice-boil-21, `tq-16` deglaze-31); `tq-11` (no ingested document
  may cover curdling — Emulsion was struck from the approved list,
  so verify `tech-sauce-15` actually supports the label).
- Vector run command (prepared, NOT run — needs paid embeddings):
  ```sh
  uv run python scripts/techniques/eval_baseline.py --mode vector --live --yes --ceiling-usd 0.01 --out evals/technique_retrieval/vector_run.json
  ```
  This uses the `.env` URL and needs `EMBEDDINGS_ENABLED=true` plus
  a key; it embeds the 16 queries (≈814 retry-inclusive tokens, see
  below) and refuses when that reservation exceeds the ceiling.
  Without `--live --yes --ceiling-usd` (or with `--fake` for a
  disposable-DB rehearsal only) it refuses. It reports HitRate@5 and
  MRR both with the 0.66 cutoff and with no cutoff, plus each
  relevant document's cosine distance. 0.66 is recipe-calibrated and
  UNCALIBRATED for technique chunks.

## Application apply package (applied 2026-09-28 with owner approval)

**Applied to `localhost:5432/culinary_copilot` on 2026-09-28** by the owner.
Recorded outcome:
- backup `data/technique-migration-backup-006.sql` (511 MB, sha256
  `c35cec6211f2f1d2…`, not in Git). The owner ran the restore check into
  `culinary_check_restore_006`; its output was not pasted into this record;
- migrate, then load;
- post-checks: ledger `001`–`007`, recipes `16033`, quarantine `443`,
  sessions `1` / session_events `1` (the 005 smoke-test row, unchanged),
  technique_documents `34`, technique_chunks `253`, null search vectors `0`;
- the tool-registry probe (`sear chicken crust`, limit 1) returned
  `fulltext` / `all`, top hit `tech-roast-03` chunk 1, with attribution
  and licence link present. Searing ranks within the top 5 for this
  query (frozen case `tq-01` is a hit).

No embeddings exist yet; `technique_embeddings` is empty until the
paid-embedding package runs. The steps below are kept as the record
of what was run and as the template for re-applying after a rollback.

Modelled on `docs/sessions.md` (the 005 record). Target: the
application database (`culinary_copilot` on `localhost:5432`, compose
service `db`, container `culinary-copilot-db-1`) only. Every command
below uses `DATABASE_URL` from `.env`; no command contains a password.

1. Target identity pre-checks (read-only; abort unless all match):
   ```sh
   uv run python -c "
   from sqlalchemy import create_engine, text
   from culinary_copilot.config import Settings
   eng = create_engine(Settings().database_url.get_secret_value())
   with eng.connect() as c:
       print('ledger:', [r[0] for r in c.execute(text('SELECT version FROM recipe_schema_migrations ORDER BY version'))])
       print('vector:', c.execute(text(\"SELECT count(*) FROM pg_extension WHERE extname='vector'\")).scalar())
       print('recipes:', c.execute(text('SELECT count(*) FROM recipes')).scalar())
       print('quarantine:', c.execute(text('SELECT count(*) FROM recipe_quarantine')).scalar())
       print('sessions:', c.execute(text('SELECT count(*) FROM sessions')).scalar())
       print('session_events:', c.execute(text('SELECT count(*) FROM session_events')).scalar())
   "
   ```
   Expected: ledger exactly 001–005, vector 1, recipes 16033,
   quarantine 443, sessions + session_events present. RECORD all six
   numbers; the post-checks compare against them.

2. Backup + restore verification (`pg_dump`/`pg_restore` live in the
   container, not on the host — same pattern as 005):
   ```sh
   docker compose exec -T db pg_dump -U copilot -d culinary_copilot \
     > data/technique-migration-backup-006.sql
   docker exec culinary-copilot-db-1 psql -U copilot -d postgres \
     -c 'DROP DATABASE IF EXISTS "culinary_check_restore_006";'
   docker exec culinary-copilot-db-1 psql -U copilot -d postgres \
     -c 'CREATE DATABASE "culinary_check_restore_006";'
   cat data/technique-migration-backup-006.sql | \
     docker exec -i culinary-copilot-db-1 psql -U copilot -d culinary_check_restore_006
   docker exec culinary-copilot-db-1 psql -U copilot -d culinary_check_restore_006 \
     -c "SELECT count(*) FROM recipes; SELECT count(*) FROM recipe_quarantine; SELECT version FROM recipe_schema_migrations ORDER BY version;"
   docker exec culinary-copilot-db-1 psql -U copilot -d postgres \
     -c 'DROP DATABASE "culinary_check_restore_006";'
   ```
   Expected on the restore before the drop: recipes 16033, quarantine
   443, ledger 001–005.
   The backup stays under `data/` (git-ignored, like the 005 backup).

3. Exact migrate command (refuses unless target matches):
   ```sh
   uv run python scripts/migrate.py \
     --expect-db-name culinary_copilot --expect-db-host localhost
   ```
   This uses `DATABASE_URL` from `.env`
   (`localhost:5432/culinary_copilot`). Expected: `applied=['006',
   '007']`, `skipped=0` on the pgvector image.

4. Exact load command (refuses unless target matches; idempotent on
   `sha256_normalized`):
   ```sh
   uv run python scripts/techniques/load.py \
     --expect-db-name culinary_copilot --expect-db-host localhost
   ```
   This uses `DATABASE_URL` from `.env`. Expected: 34 docs / 253 chunks.

5. Post-checks (read-only, then one tool probe; abort/roll back on
   mismatch — tests never touch the app DB, so no `make check` here):
   - `SELECT version FROM recipe_schema_migrations` is
     `001`–`007` with unchanged `001`–`005` checksums (the runner
     fails closed on any byte change; `git status` shows no
     modification to the migration files);
   - `recipes`, `recipe_quarantine`, `sessions` (+ `session_events`)
     counts identical to the recorded pre-checks;
   - `SELECT count(*) FROM technique_documents;` is 34;
   - `SELECT count(*) FROM technique_chunks;` is 253;
   - `SELECT count(*) FROM technique_chunks WHERE search_vector IS
     NULL;` is 0;
   - one full-text probe through `search_techniques` via the tool
     registry:
     ```sh
     uv run python -c "
     import asyncio
     from sqlalchemy import create_engine
     from culinary_copilot.config import Settings
     from culinary_copilot.tools import all_tool_definitions, all_tool_impls, run_tool
     from culinary_copilot.tools.registry import ToolContext
     settings = Settings()
     eng = create_engine(settings.database_url.get_secret_value())
     defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
     ctx = ToolContext(settings=settings, engine=eng)
     out = asyncio.run(run_tool(defs['search_techniques'], all_tool_impls()['search_techniques'], {'query': 'sear chicken crust', 'limit': 1}, ctx))
     assert out['ok'] and out['results'], out
     hit = out['results'][0]
     assert hit['attribution_text'] and hit['licence_url'], hit
     print('probe:', out['mode_ran'], out['match'], hit['doc_id'], hit['chunk_id'])
     "
     ```

6. Rollback (targeted SQL undo, rehearsed on disposable DB
   `culinary_check_rollback_006`: 34 docs + 9 fake 007 vectors in,
   ledger back to `001`–`005`, technique tables and `tech-` run rows
   gone, sessions intact — run in one transaction, only with owner
   approval):
   ```sql
   DROP TABLE IF EXISTS technique_embeddings;
   DROP TABLE IF EXISTS technique_chunks;
   DROP TABLE IF EXISTS technique_documents;
   DELETE FROM embedding_runs WHERE run_id LIKE 'tech-%';
   DELETE FROM recipe_schema_migrations WHERE version IN ('006', '007');
   ```
   Then re-verify the counts from step 1 (ledger `001`–`005`,
   recipes/quarantine/sessions unchanged, no `technique%` tables).
   Session `evidence` may keep historical technique refs (plain JSON,
   no foreign key — harmless record of what was cited). The backup
   restore from step 2 is the last resort only: a restore would also
   lose any sessions created after the backup.

## Paid-embedding run (executed 2026-09-28 with owner go-ahead)

Outcome: 253 chunks in 4 requests on the app DB (ledger
`data/embeddings-technique/app-run-1`, git-ignored). Recorded usage
figures are pre-fix estimates (see "Usage reporting" above), not
provider-reported: reservation 421,108 tokens (210,554 per attempt).
The package below is the record of what was run.

- Corpus reservation from the real renormalized corpus (dry-run on
  the rehearsal DB): **421,108 tokens (retry ×2), $0.00842** at
  `text-embedding-3-small` $0.02/1M
  (`embeddings/registry.py`, `EMBED_PRICING_VERSION
  2026-09-24-embed-v1`; re-verify pricing before running and bump the
  version if it changed).
- The vector eval embeds the 16 queries as a separate small paid call:
  814 retry-inclusive tokens ≈ **$0.00002**. Combined with the corpus
  (421,108 + 814 = 421,922 tokens) ≈ **$0.00844 — 84.4% of the $0.01
  cap** (~0.8% of the $1.00 M3 ceiling; the $0.15 Phase 3 live run is
  still unapproved and untouched by this).
- Stop condition: `EMBED_BUDGET_USD` (set ≥ $0.01) **plus**
  `--ceiling-usd 0.01`; the CLI refuses when the retry-inclusive
  reservation exceeds the ceiling, and `--live` additionally requires
  `OPENAI_API_KEY` + `--yes`.
- Exact command (target: pgvector application DB after the apply
  package above; uses `DATABASE_URL` from `.env`, no password):
  ```sh
  EMBED_BUDGET_USD=0.01 uv run python scripts/techniques/embed_techniques.py \
    --live --yes --ceiling-usd 0.01 \
    --expect-db-name culinary_copilot --expect-db-host localhost \
    --run-dir data/embeddings-technique/app-run-1
  ```
- Post-check: `embedding_runs` row `tech-app-run-1` is `done`;
  `SELECT count(*) FROM technique_embeddings;` (= 253); spot
  `technique_vector_candidates` probe; then run the vector eval
  command above (its 16 query embeddings are the $0.00002 call).

## Phase 4 results (recorded; owner ran the paid steps 2026-09-28)

- Paid embedding run on the app DB, 2026-09-28: 253 chunks in 4
  requests (technique run ledger `data/embeddings-technique/app-run-1`,
  git-ignored). Recorded figures are pre-fix estimates-as-usage (see
  above): reservation 421,108 tokens; real billed usage unknown
  (~52k expected).
- Vector eval (`evals/technique_retrieval/vector_run.json`,
  2026-09-28, same 16 frozen cases): **0.938 / 0.844 with the 0.66
  cutoff and 0.938 / 0.844 without it**, against full-text 0.812 /
  0.781. The cutoff did not bind: first-relevant distances span
  0.39–0.61, all below 0.66 (only tq-06's second label reaches
  0.737, still inside its top-5).
- Per-case vector vs full-text (hit / RR; vector RR identical with
  and without cutoff):
  - tq-01: 1/0.50 → 1/1.00 (sear top, d=0.575). Rank fixed.
  - tq-02: MISS → 1/1.00 (braise, 0.417). Fixed.
  - tq-03: 1/1.00 → 1/1.00. Same.
  - tq-04: MISS → 1/1.00 (fda-safe-32, 0.393). Fixed (Phase 7 case).
  - tq-05: hit → 1/1.00. Same.
  - tq-06: hit → 1/1.00. Same.
  - tq-07: 1/1.00 → 1/0.50 (fry-07 first). Rank drop.
  - tq-08, tq-09, tq-10: 1/1.00 → 1/1.00. Same.
  - tq-11: hit → 1/1.00 (sauce-15, 0.609). Same.
  - tq-12, tq-13: 1/1.00 → 1/1.00. Same.
  - tq-14: 1/1.00 → 1/0.50 (baking-26 first). Rank drop.
  - tq-15: MISS → MISS. Both miss (Phase 7 case).
  - tq-16: 1/1.00 → 1/0.50 (sauce-15 first). Rank drop.
- Caveats: 16 AI-drafted cases with document-level labels; queries
  were shortened after an exploratory pre-freeze run (honesty record
  in `cases.json`); the 0.66 cutoff is recipe-calibrated and did not
  bind here, so it stays uncalibrated for technique chunks.
- `TECHNIQUE_RETRIEVAL_MODE` default unchanged (`fulltext`). The
  mode decision is deferred to the Phase 7 agent-choice comparison
  (Checkpoint 0 decision 2 pattern: the agent picks per query).

## Planned vs implemented

- Implemented, applied, and measured: fetch/normalize v2 (34/40),
  006/007 (applied to the app DB 2026-09-28), loader, chunking
  (renderer v2), embedding CLI (usage reporting fixed after the paid
  run; historical rows keep estimates), full-text `search_techniques`
  with all/any match, validation/evidence, attribution, frozen eval +
  full-text baseline (0.812/0.781) + paid vector eval (0.938/0.844
  both cutoffs).
- Planned, not implemented: the Phase 7 agent-choice mode comparison
  (carries the tq-04/tq-15 food-safety regression cases below), any
  redistribution of corpus text (owner decides; share-alike would
  then apply to redistributed adaptations).

## Phase 7 carry-over: food-safety regression cases

- tq-04 "chicken internal temperature": full-text misses it (the FDA
  page says "poultry", never "chicken"); vector finds
  `tech-fda-safe-32` top-1 (d=0.393).
- tq-15 "pink chicken inside": both modes miss it; the FDA page says
  colour is not a reliable doneness indicator, so the correct agent
  behavior is a thermometer answer, not a document hit.
