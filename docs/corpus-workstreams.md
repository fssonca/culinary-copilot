# Historical corpus workstreams 1–3 (2026-09-22)

This records the initial workstreams, not current deployment status. Counts,
versions, test results and “unapplied” statements below describe that stage.
The later Foodie migration and API implementation supersede those statuses:
migrations 001–003 are applied locally, the corpus contains 16,033 recipes,
and API search defaults to both datasets. See [current import guidance](recipe-ingestion.md)
and [hybrid ingestion](hybrid-ingestion.md) for maintained workflows.

## What changed

- **1A — keyword indexing**: fixed `*" ".join(keywords)` character expansion
  in `import_data.persist`. New versioned renderer
  (`recipes/search.py`, `SEARCH_DOCUMENT_VERSION = "1"`) preserves whole
  keyword phrases and renders title + description + category + keywords +
  ingredients + instructions. New idempotent rebuild CLI
  (`recipes/rebuild_search.py`); new additive migration
  `migrations/002_search_version.sql` (**not applied** to the app DB).
  Retrieval is dataset-aware (`search_all`, optional `dataset_id`) with the
  Food.com default preserved, so `GET /api/v1/recipes/000038` keeps working.
- **1B — nutrition**: kept legacy `nutrition: {fat: …}` float map untouched;
  added `nutrition_observations` (nutrient, value, unit, basis, serving
  amount/unit, source field/raw, reported-vs-calculated, verification,
  evidence + URLs). AkashPS11 columns stay `unit/basis = unknown`,
  `verification = unverified` (no upstream statement; field-name inference
  refused). Malformed/nonfinite/negative values are flagged (`warning`),
  never silently nulled; valid zeros preserved. No ingredient-based
  calculation; diet keywords never imply compliance.
- **1C — quality**: `quality_issues` (code/severity/field/message),
  `available_fields`, and `capabilities` (searchable, evidence_usable,
  quantities_validated, complete_eligible, scalable) are now first-class.
  Reports count `defective_rows` from warning/error issues only — presence
  metadata (`images_present`, `nutrition_present`) never counts as defective.
  Legacy `flags` list retained for API compatibility. Source durations kept
  as reported; no active/resting split inferred.
- Normalizer `VERSION` is now `"3"` (v2 records in the app DB untouched).
- **Workstream 2**: `recipes/adapters/` (shared `base`, `foodcom` wrapper,
  `foodie` parser) + `scripts/datasets/foodie_pilot.py` (development tool;
  shared checksum/sampling helpers in `recipes/dataset_utils.py`). Pilot artifacts (ignored):
  `data/odunola-pilot/{normalized.jsonl, quarantine.jsonl,
  quality-report.json, pilot-manifest.json, REVIEW.md, review-template.csv}`.
  Foodie IDs are deterministic `foodie-{row:06d}` for the pinned
  revision+checksum; reordering upstream would remap them (documented in the
  adapter). `source_url` is always null (never invented); license declared
  `apache-2.0` from card front-matter with `provenance_status: pending`.
- **Workstream 3**: `scripts/datasets/jojogo9_audit.py` (bounded safe parser) and
  `docs/jojogo9-provenance-coverage.md`. Recommendation: **HOLD** (no
  license/card; conflicting serialization on 700 overlapping IDs; no
  quantities/servings). No bulk import performed.

## What was executed

- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/`.
- Offline tests: `uv run pytest -q` (35 passed, incl. 12 new workstream
  tests + 4 isolated-DB tests when PostgreSQL is reachable; skipped
  otherwise — no creds/downloads required).
- Isolated-DB tests create/drop only `culinary_test_isolated`; app DB
  (1,216 recipes) untouched.
- Foodie pilot: `uv run python scripts/datasets/foodie_pilot.py
  --size 150 --seed 42` → 144 accepted / 11 defective / 6 rejected.
- jojogo9 audit: `uv run --with pyarrow python
  scripts/datasets/jojogo9_audit.py` (231,637 rows exact scalars;
  2,000-row sampled parse).

## Commands retained from the initial workstreams

```bash
# 1. Preview current-corpus normalization (no DB writes):
uv run import-recipes --output data/recipe-preview

# 2. Apply schema + import to a chosen database (NOT the current app DB
#    without separate review). Points at $DATABASE_URL:
uv run import-recipes --write --apply-schema --output data/recipe-import

# 3. Rebuild search_text idempotently (dry-run first):
uv run python -m culinary_copilot.recipes.rebuild_search \
  --dataset AkashPS11/recipes_data_food.com --dry-run
uv run python -m culinary_copilot.recipes.rebuild_search \
  --dataset AkashPS11/recipes_data_food.com --apply

# 4. Re-run the foodie pilot (deterministic; artifacts ignored):
uv run python scripts/datasets/foodie_pilot.py --size 150 --seed 42

# 5. Re-run the jojogo9 read-only audit:
uv run --with pyarrow python scripts/datasets/jojogo9_audit.py
```

Migration `002_search_version.sql` is additive (`ADD COLUMN IF NOT EXISTS`
with default `'1'`) and ships unapplied; `persist` and `rebuild` work with
or without it (version falls back to the document provenance).

## API compatibility

- `GET /api/v1/recipes/000038` unchanged; `get_recipe(engine, "000038")`
  resolves the Food.com record as before, with cross-dataset fallback.
- `document.nutrition` type unchanged; `nutrition_observations`,
  `quality_issues`, `available_fields`, `capabilities`,
  `search_document_version` are additive.
- `document.flags` retained (deprecated mix); new consumers should use
  `quality_issues` + `available_fields`.

## Dataset findings (summary)

- Foodie pilot: amount coverage 0.97 / unit coverage 0.79 (coverage ≠
  accuracy); servings explicit in only 3/144; durations in 35/144; 22
  recipes with section headings; 3 duplicate candidates; 6 rejections are
  non-conforming text shapes (no Ingredients/Directions markers).
- jojogo9: 231,637 rows, unique IDs, 0 sampled parse failures, all
  nutrition lists length 7, `minutes` anomalies incl. `INT_MAX`; 700 of our
  1,216 IDs overlap with conflicting serialization (ID 38: 13 vs 9 steps);
  no quantities/servings; license missing → HOLD.
- Human review needed: label 30 pilot rows via
  `data/odunola-pilot/review-template.csv` (~1.5 h) before any accuracy
  claim; decide nutrition unit/basis evidence bar; decide version
  precedence for the 700 colliding IDs if jojogo9 is ever pursued.

## Hybrid ingestion (new)

Deterministic parser v2 + resumable OpenAI Batch extraction. Full runbook:
`docs/hybrid-ingestion.md`. Pilot: 44/150 routed to the LLM (reasons
reported), 106 deterministic, 0 quarantined. Batch prepared offline only —
**nothing submitted**; exact live command is `submit --yes` in the runbook.
New versioned artifacts under `data/odunola-hybrid/`; original pilot and
review files untouched.
