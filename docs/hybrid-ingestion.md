# Hybrid ingestion runbook: deterministic parsing + OpenAI Batch extraction

The LLM interprets ambiguous source text inside the ingestion workflow.
Deterministic scripts own parsing, routing, validation, merging,
provenance, caching, indexing and database transactions. No autonomous
agent, no web research, no recipe generation.

## 0. Configure (once)

```bash
test -f .env || cp .env.example .env   # preserve existing settings; never commit .env
```

| Variable | Meaning | Default |
| --- | --- | --- |
| `LLM_INGESTION_ENABLED` | master switch | `false` |
| `OPENAI_API_KEY` | secret (never logged) | empty |
| `LLM_EXTRACTION_MODEL` | pinned model from `llm/models.py`; CLI refuses silent substitution | `gpt-6-luna` |
| `LLM_REASONING_EFFORT` | Responses reasoning effort, checked against the model at startup (`gpt-6-luna`: `none`/`low`/`medium`/`high`/`xhigh`/`max`); blank = server default (`medium`) | empty |
| `LLM_BATCH_REQUEST_LIMIT` | max requests per prepare/retry | `200` |
| `LLM_MAX_OUTPUT_TOKENS` | per-request output cap | `8000` (measured floor on gpt-5-nano, historical: 5120/6220 output tokens went to reasoning for a 9-line recipe; 2000 always truncated; not re-measured on gpt-6-luna) |
| `LLM_RETRY_LIMIT` | attempts before a record goes unresolved | `2` |
| `LLM_AUDIT_SAMPLE_RATE` / `LLM_AUDIT_SEED` | deterministic audit sample of passed records | `0.0` / `20260707` |
| `LLM_BUDGET_USD` | spend ceiling on the batch-discounted estimate | empty (check off) |
| `LLM_PRICE_INPUT_PER_1M` / `LLM_PRICE_OUTPUT_PER_1M` | sync list prices | empty = cost unknown |

Pricing assumptions (verified 2026-09-24): gpt-6-luna Standard list prices
input $0.10 / output $0.50 per 1M tokens (cached input $0.01, cache writes
$0.125; <https://developers.openai.com/api/docs/pricing?latest-pricing=standard>);
the estimator applies the Batch discount documented in
<https://developers.openai.com/api/docs/guides/batch> (50% of sync when
verified on 2026-09-22 for gpt-5-nano; re-check the Batch price for
gpt-6-luna before a submit). Earlier runs and their costs used gpt-5-nano
at $0.05 / $0.40.
Estimates are labeled estimates, never exact. With prices unconfigured the
estimator reports `unknown` and submit requires `--limit` plus `--yes`.

Compatibility verified 2026-09-24: `gpt-6-luna` supports Batch, `/v1/responses`
and structured outputs (the loaded corpus was extracted with `gpt-5-nano`,
verified 2026-09-22; a batch run prepared with that model cannot be resumed
under `gpt-6-luna`); batch bodies use `/v1/responses` with a strict
`json_schema` text format (`recipe_extraction`; current local schema v5, prompt v6).
The targeted extraction contract introduced in v4 remains in use: scripts preserve title, description, steps,
servings, durations and notes, and the model resolves only flagged
ambiguous lines/blocks (contradicting settled fields or paraphrasing
re-emitted steps is a major failure). Merge overlays per-line with
`origin` tags and unions model/deterministic groups. Validation problems
carry `critical`/`uncertainty`/`optional` classes: prose/metadata omissions are
reported but never affect verdicts or capabilities; faithfully preserved
ranges and honestly absent amounts report as `uncertainty` (not errors) while
still capping the verdict at partial with restricted capabilities; genuine
quantity/unit/identity/instruction/servings failures stay `critical`.
Strict mode requires every property key in `required` and every array with
`items` — two live rejections taught us this (see
`data/odunola-hybrid/live-evaluation.md`). Requests carry the full content
hash for exact-echo validation; model unit spellings canonicalize against
our closed alias table in scripts.

## 1. Prepare ingestion and inspect routing (offline, no network)

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run \
  prepare --size 150 --seed 42 --audit-rate 0.05
```

- Runs deterministic parsing (adapter v2) + routing on the pinned
  `odunola/foodie` revision; verifies the CSV SHA-256.
- Writes `batch-input.jsonl`, `mapping.json` (custom_id → source context),
  `sources.json` (every selected record), `manifest.json`, `routing.json`
  under the run dir (all ignored by git).
- Prints dataset/revision, route counts + reasons, request count, model,
  estimated tokens/cost, caps and limits. Refuses when requests exceed
  `--limit` or the model differs from configuration.
- Optional `--route-unstructured-durations` also routes records whose
  timings are mentioned but unstructured (default off; 47/150 in pilot).
- Optional `--rows 303,3423` selects explicit source rows instead of
  sampling; `--reasoning-effort low` sets Responses reasoning effort;
  `--experiment NAME` / `--supersedes RUN` label a new experiment linked
  to an original (attempts restart, explicitly marked — never disguised
  as an automatic retry).
- Custom IDs are stable: `{source}:{hash12}:p{prompt}s{schema}[:seg]` —
  re-preparing identical inputs yields identical IDs.

Pilot result: 44/150 routed (20 `name_contains_measure`, 17
`quantity_unknown`, 6 `section_boundary_failed`, 6 `audit_sample`;
106 deterministic, 0 quarantined).

## 2. Submit the batch (explicit paid action — NOT executed here)

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run \
  submit --yes
```

- Re-displays dataset/revision/requests/model/estimate/caps and requires
  `--yes`; enforces the budget ceiling and the prepared model.
- Uploads with `purpose="batch"`, persists the file ID **before** creating
  the batch, then persists the batch ID. A manifest that already has a
  batch ID refuses resubmission — run `status` to reconcile instead.
- Interrupted after upload but before creation? Re-run with `--resume` to
  reuse the persisted file ID rather than uploading twice.
- The attempt is persisted **before** batch creation. If creation raises
  (timeout/crash), rerunning `submit` refuses and requires
  `status --reconcile`, which lists server-side batches, adopts the one
  referencing our file (matched by `input_file_id`), or marks the attempt
  absent so submission may proceed. Duplicate batches are never created
  by guessing.
- Exact next live command is the block above (after setting
  `LLM_INGESTION_ENABLED=true`, `OPENAI_API_KEY`, and prices in `.env`).

## 3. Check status (retrieves once; never blocks for hours)

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run status
# optionally: status --wait 300   (bounded, polls every 30 s, max 600 s total)
```

Statuses: `validating → in_progress → finalizing → completed`, plus
`failed / expired / cancelling / cancelled`. Every observation is appended
to `manifest.json → status_history`.

## 4. Collect results (matches strictly by custom_id)

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run collect
```

- Downloads output + error files; detects unknown, duplicate and missing
  result IDs (order is never trusted).
- Handles refusals, incomplete responses and malformed structured output
  as `model_failure` entries in `result-errors.jsonl`.
- Records per-request usage, actual tokens and estimated actual cost
  (including retries on later passes) in `collect-report.json`.

## 5. Finalize validation (offline)

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run finalize
# cache lives in data/llm-cache/ (ignored); override with --cache-dir
```

- Verdict ladder — detected errors always affect acceptance:
  **fatal** (identity/staleness/fabricated evidence/invented
  servings/yield/URLs) rejects without retry; **major** (amount mismatch,
  nonfinite/nonpositive amounts, unsupported units/temperatures, invented
  alternatives, empty/unknown evidence, servings/batch numbers that match
  no source statement, missing boundaries, order violations) rejects with
  bounded retry; coverage gaps and uncertainty cap at `accepted_partial`;
  only a clean `resolved` response is `accepted`.
- Servings/batch counts must numerically match source statements
  ("Serves 2" can never justify 999; "2 dozen" must be exactly 24).
  Capabilities are RECOMPUTED and additionally gated: anything but a clean
  `accepted` caps at evidence-only (no validated quantities, completeness
  or scalability); uncertain amounts never validate.
- Merges and writes `ready_to_load.jsonl` (deterministic successes +
  accepted LLM interpretations), `final-quarantine.jsonl`,
  `validation-report.json`, and `records.jsonl` covering **every selected
  record** (deterministic, LLM-routed, quarantined). Refuses when code
  versions changed since prepare — re-prepare instead of mixing.
- Caches validated results under a key covering dataset, revision, row,
  content hash, adapter/routing/prompt/schema versions, prompt hash and
  model — unchanged re-runs reuse cache; any change invalidates. Cache
  entries additionally record validator/merge logic versions
  (`VALIDATOR_VERSION`/`MERGE_VERSION` in `llm_validate.py`); finalize
  reuses a cached merge only when both match current code. Legacy or
  older-logic entries are ignored and the saved raw response is
  revalidated/remerged automatically — stale merges can never bypass
  corrected checks, and a miss never triggers a paid request (finalize
  is offline). Bump the logic version with any validation/merge rule
  change; never bump prompt/schema versions for logic fixes.

## 6. Retry eligible failures (offline prep; bounded)

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run \
  retry --limit 50
```

Only `awaiting_llm` records with attempts remaining are included (request
errors, model failures, expired, missing results). When the budget is
exhausted, finalize moves the record to terminal `unresolved` with an
explicit reason (`output_budget_exhausted` for reasoning cutoffs,
`retry_budget_exhausted` otherwise) — raw inputs, responses, validations
and attempt history are preserved, attempts are never reset through retry
dirs or reconciliation (reconcile keeps the max), and unresolved records
stay out of `ready_to_load`. Validated, `not_a_recipe` and
stale-configuration records are never retried from here.

A retry output is a **full run directory** (`batch-input.jsonl`,
`mapping.json`, `sources.json`, `manifest.json` with a `parent_batch`
link), so the normal commands work on it unchanged:

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run/retry-<ts> submit --yes
# ... status / collect / finalize as usual ...
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run \
  reconcile --retry-dir data/odunola-hybrid/run/retry-<ts>
```

`reconcile` merges child states into the parent run (child wins for
retried sources), appends new ready rows (deduped), extends the validation
report, and adds retry usage/costs to the parent collect report. It is
idempotent: re-running it for the same retry directory skips with a message
instead of double-counting.

## 7. Review output

- `data/odunola-hybrid/deterministic-report.json` — v2 deterministic
  counts, capabilities, and the adjudication table for every 2026-09-22
  review label (`fixed / partial / spec-clarified / deferred-to-llm /
  source-limitation` with notes).
- `data/odunola-hybrid/before-after-30.json` + `BEFORE-AFTER-30.md` —
  adapter v1 vs v2 on the reviewed 30.
- `data/odunola-hybrid/review-bundle-fresh30/` — 30 NEW pilot rows (seed
  20260801, original review 30 excluded) with source text + deterministic
  output; `llm_status: pending` everywhere (pending is not completion).
- `data/odunola-hybrid/run/routing.json` — per-record routes + reasons.
- `llm_batch compare --run-dir <dir>` — readable original-vs-extracted
  markdown (`comparison.md`): per-line coverage marks, normalized
  model-vs-reference quantity/unit diffs, step order. Review with
  `review-liveassistant-*.md` (AI-assisted passes, not human review).
- `tests/fixtures/llm_expected/` — AI-authored reference fixtures
  (pending human review) pinning the targeted contract through the real
  validator+merge (`tests/test_llm_fixtures.py`); per-record
  original/expected/actual/reason analysis in
  `data/odunola-hybrid/fixture-comparison.md`.

## 8. Load finalized records later (explicit; NOT run here)

```bash
uv run python -m culinary_copilot.recipes.llm_batch --run-dir data/odunola-hybrid/run \
  load --import-id foodie-hybrid-001
```

- Upserts `ready_to_load` rows scoped to `odunola/foodie`
  (`ON CONFLICT (dataset_id, source_id) DO UPDATE`); other datasets are
  never touched and no snapshot is replaced.
- Refuses when any record is still pending; `--partial` loads only ready
  rows non-destructively. No transaction is held open during batch
  processing — `load` is a separate short transaction.
- Fully-rejected rows persist in the same pass: routing quarantines from
  `final-quarantine.jsonl` (`status='quarantined'`) and terminal validator
  rejections from `records.jsonl` + `validation-report.json`
  (`status='rejected'`, primary reason, full problem list), upserted on
  `(import_id, row_number)`. Needs migration `003_quarantine_status.sql`.
- Apply migrations without importing: `load --apply-schema` reuses the
  importer's checksum-pinned `apply_migrations()` (fresh DBs get 001–003;
  reruns apply nothing; existing rows survive with column defaults).
  Retry runs inherit `reasoning_effort` (a rehearsal caught all retry
  results going `stale_configuration` without it).
- Amount/unit source-consistency: structured units must be evidenced in
  the cited line (synonym-aware, `count` exempt, short aliases
  whole-token-only); every reported number must occur in the line as an
  exact rational. Violations (`unit_unsupported_by_line`,
  `amount_unsupported_by_line`) are major and retry-eligible. Merge never
  inherits prior amounts/units onto lines shared by several model
  ingredients (glued-source leakage, e.g. 19380's `lb`).
- Cache/version implication: `cache_key` covers adapter/routing/prompt/
  schema/model versions; validator/merge logic versions travel inside
  each cache entry and are checked on every read (`entry_is_fresh`).
  After logic fixes, simply re-finalize — stale entries are ignored and
  raw saved responses revalidated automatically (verified on 148 legacy
  tranche entries: 0 reused, 135/65 reproduced exactly). No prompt/schema
  bump for logic fixes.
- Verified lifecycle (final-check-20260922): copy run artifacts, finalize
  parent → finalize retry child → reconcile → `load --partial
  --apply-schema` into a disposable database. Awaiting rows stay pending
  (excluded from both tables, block non-partial loads); unresolved rows
  persist as quarantine with reason/verdict/problems/raw; reloads are
  idempotent; a poisoned ready row aborts the whole import atomically.
- Offline rehearsal procedure (`data/odunola-hybrid/rehearsal-20260922/`):
  `build_replay.py` converts saved direct responses into Batch-shaped
  collect outputs (originals preserved, `batch_id` null, labeled replay),
  then production `finalize` → `retry` → child `finalize` → `reconcile` →
  `load --apply-schema` against a disposable database. Verifies the 45/45
  accounting, attempt preservation, capability restrictions, idempotent
  reloads, pending-block, and atomic rollback.

## Where source text goes, what is retained, how to resume

- Sent to OpenAI on submit: the `texts` of routed records only (44/150 in
  pilot), with line IDs, fallible parse hints labeled as such, and routing
  reasons — via Files (`purpose=batch`) + one Batch. Nothing else uploads.
- Retained locally: raw CSV (HF cache), run dir (requests, mapping,
  manifests, results, validations, merged output), `data/llm-cache/`
  (validated interpretations), original pilot + review files (untouched).
- Resume after interruption: re-run the same command — `prepare` is
  deterministic, `submit --resume` reuses the uploaded file, `status` /
  `collect` / `finalize` read the persisted manifest. State is per run
  directory; retry batches link back via `parent_batch`.

## Evaluation

Compare, where available: (1) original deterministic parser (v1 pilot
artifacts), (2) corrected parser (v2 `data/odunola-hybrid/`), (3) corrected
parser + LLM fallback (pending live run). Metrics per stage: critical
ingredient/quantity/unit errors, missing occurrences, instruction fidelity,
metadata completeness, unsupported capability claims, routing rate/reasons,
recovery/abstention/quarantine rates, model-introduced errors,
requests/tokens/cost per accepted record. Live performance is measured only
in `data/odunola-hybrid/live-evaluation.md` (2 validated accepts of one
record, small samples — do not generalize); mocked batch results exercise
the machinery only and must never be presented as model performance.

## Semantic-validation limitations (honest)

Evidence-span checks + schema validity prove resolvability, not truth: a
well-formed interpretation with real citations can still attach the wrong
quantity to an ingredient when the source is genuinely ambiguous. Defense
in depth: coverage/order requirements, number-allow-listing, invented-field
rejection, capability recomputation, explicit `uncertain`/`unresolved`
retention, audit sampling of passed records, and quarantine over guessing.

## Contract correction: prompt v6 / schema v5

Targeted requests now carry `requested_ingredient_line_ids`; deterministic
coverage satisfies untouched ingredients. Explicitly unclassified requested
lines remain partial. The request, validator and merge use the same scope.
The unit schema exposes the accepted vocabulary; numeric values must contain
numbers only. Exact mixed fractions share one parser in validation and merge.
Source-backed `container` is retained as a package kind, never changed to `can`
or converted to mass/volume. Package size stays in source text/notes.

Source IDs align original occurrences through Unicode normalization, consuming
duplicate lines in order. Original evidence stays unchanged. Merge preserves
untouched ingredients and their order, never mutates the baseline, and does not
erase known units with model nulls. Unresolved ambiguous quantities stay unknown.
Failed deterministic parses no longer reuse the previous record's baseline.

Single-line scrapes currently require explicit abstention. They remain unresolved
and are excluded from loading; recovering them needs a future character-span
contract. Do not paraphrase a whole-line evidence rule into acceptance.

Existing run manifests/caches are stale under these versions. Re-prepare before
any new submission. Historical responses may be replayed **diagnostically**,
with both stale production verdicts and counterfactual current-rule verdicts
recorded. Replay does not authorize loading or establish new model performance.

## Routing guard (routing v2)

Ordinary extraction requires an ambiguous ingredient or a source-backed,
recoverable missing field. Broad measurement-name warnings alone no longer
trigger a call when the targeted scope is empty (e.g. cut-size descriptions).
The `no_recoverable_targets` decision retains suppressed signals for audit and
does not upgrade recipe quality or capabilities. Missing optional servings alone
is insufficient; a numbered servings/portions statement must exist in the source.
Structural dropped-line signals still route conservatively rather than silently
accepting potentially incomplete data.

Unsupported single-line scrapes are quarantined locally with
`single_line_requires_spans`, preserving raw input. They are never sampled for
LLM audit. Source-alignment failures also remain local in quarantine.

Audit sampling is opt-in through `--audit-rate`; requests explicitly carry
`audit: true` and brief `mode: audit`, with all mapped ingredient lines as the
requested scope. This remains separate from ordinary targeted fallback.

A zero-request prepared run can be finalized offline immediately. `submit`
refuses before creating an API client or uploading a file. Routing v2 invalidates
previously prepared runs; prepare into a fresh directory. No live pilot was run
to verify this guard.

## Completed Foodie migration (2026-09-23)

The application contains 16,033 recipes (1,216 Food.com + 14,817 canonical
Foodie) and 443 quarantine records (12 existing + 431 Foodie). Migrations
001–003 are applied. Boundary sources remain on hold.

The retained migration package, backup, alias map and verification evidence are
in `data/odunola-hybrid/migration-grams-v4-20260923/`; start with
`APPLICATION-MIGRATION.md` for results and recovery instructions. The final
`staging-run/` package is preserved with its recorded hashes. Superseded packages,
one-off scripts and redundant intermediate copies were removed after completion.
Do not rerun the completed application migration.

### Current repair and validation rules

Adapter 4 removed generic hyphenated/doubled-g prefix splitting that corrupted
`gluten-free` and `garlic-stuffed`. Gram evidence is checked against original text
independently of parser repair, using standalone units and a bounded vocabulary
of known glued forms. Ambiguous observed forms stay unresolved. Validator and
merge logic versions are 3; older cached interpretations are invalidated.

`merged_ingredient_problems()` checks amounts and units for all origins,
including deterministic fields. Finalize and load enforce this gate. These
checks do not prove semantic attribution on shared ingredient lines. The
retained `gram-audit.json` covers 236 prior canonical gram occurrences, five
alternate occurrences and all 219 current gram occurrences, with no unsupported
gram fields in the admitted package. Capability restrictions remain unchanged.

### Exact duplicates and source lookup

Deduplication uses equality of raw source text, not parsed content hashes.
Canonical ID is the lowest source ID in each exact-text group. `source_records`
preserves original IDs, row numbers, provenance and capabilities;
`duplicate_interpretations` retains alternate documents. `aliases.json` maps
18,027 admitted sources to 14,817 canonical recipes (3,210 alternates).
Different source texts are never collapsed. Alias IDs are metadata, not
independent API lookup IDs; resolve them to canonical IDs when needed.
