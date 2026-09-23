# Recipe import review and Milestone 1 assessment

“Week 1” means **Milestone 1 — Backend and Data Foundation** in the revised plan.
No calendar deadline is assumed.

## Current state (2026-09-23)

The local application migration is complete: 1,216 Food.com + 14,817 Foodie
recipes, 443 quarantine records, and migrations 001–003 applied. Imports are
explicit CLI operations; a fresh checkout does not include the local database
or ignored data artifacts. API search supports both datasets by default and
explicit dataset filtering; legacy Food.com lookup remains supported.

This guide describes the **Food.com importer**. For Foodie routing, LLM
extraction, validation and loading, use [the hybrid ingestion runbook](hybrid-ingestion.md).
The importers have different update policies; do not apply the snapshot-replacement
instructions below to Foodie's upsert loader.

Clarification handling, reviewed enrichment, recipe generation and retrieval
benchmarking remain future work. Missing units and dietary evidence must not
be inferred as facts. The ingestion LLM pipeline exists; application recipe
generation does not.

## Process summary

1. Download `recipes.csv` from the pinned Hugging Face dataset revision, or use
   `--csv PATH`. Verify the audited SHA-256 before parsing or connecting to Postgres.
2. Stream CSV records. Count empty/barcode-only placeholders; preserve their original
   bytes in the downloaded source file instead of creating a million empty DB records.
3. Parse R-style lists without evaluating code. Preserve IDs (including leading zeros),
   original ingredient wording, ordered instructions and every raw field.
4. Normalize case/spacing, parse supported durations into minutes, parse exact numeric
   quantities as rational strings, and optionally map exact Epicure vocabulary tokens.
5. Quarantine records with missing identity, unusable lists, incomplete instructions or
   duplicate IDs. Flag quantity mismatches, malformed duration values, unknown quantities,
   unmapped ingredients and duplicate content. CSV/header/checksum errors abort the run.
6. Produce `report.json`, `normalized.jsonl`, and `quarantine.jsonl` in the output directory.
   The report includes counts, reasons, missing-field rates and ingredient mapping coverage.
7. Only with `--write`: use one PostgreSQL transaction for the migration (if requested),
   import report, quarantine and current recipe snapshot. Failures roll back the transaction.
   A database lock serializes imports. Re-running produces the same logical records.

The current dataset snapshot is **replaced**, not appended: records no longer accepted
are removed for this dataset only. Historical reports/quarantine remain; historical
accepted snapshots are not retained in Postgres. Keep downloaded source files and preview
outputs for reproducibility. Future tables referencing recipes will require revisiting
this snapshot replacement strategy.

## Conservative decisions to review

- Dataset: [AkashPS11/recipes_data_food.com](https://huggingface.co/datasets/AkashPS11/recipes_data_food.com).
- Pinned revision: `aa68f5bf9c9f33a9fe4624e180d9e80a2030c675`.
- CSV checksum: `3f93a145e5449fcd1ddd9c90896b669f8dc17bb2b37f8781c05179dd3765efe7`.
- Prior audit: 1,228 candidate recipes out of 1,048,543 rows. This is historical evidence,
  not a claim that this new importer has accepted all 1,228; inspect its actual report.
- The source has no reliable ingredient unit column. `unit=null`, `scalable=false` and
  `final_recipe_eligible=false` apply to every imported recipe. These records are useful
  retrieval evidence, but require reviewed enrichment before complete recipe generation.
- Mismatched quantities are preserved in the raw record and assigned to **no ingredients**.
- Canonical names are lexical normalization only. No automatic singularization, aliases,
  dietary/allergen certification, ingredient role inference or nutrition claims.
- `--vocab` accepts a local Epicure Core `vocab.json` object. Only exact normalized-token
  matches are mapped; this does not download models or verify culinary equivalence.
- Duplicate content means equal normalized title/ingredient names and ordered instructions.
  Different IDs remain available with flags; no fuzzy deduplication is attempted.
- No verified recipe URL column exists; `source_url` remains null. Dataset revision,
  source ID and raw author fields provide attribution without inventing links.
- Dataset card declares MIT, but upstream Food.com reuse/provenance needs further review
  before public redistribution.

## Food.com import commands

For a new import or deliberate refresh, run from the repository root.
The already-migrated local corpus does not require another import:

```bash
uv sync --locked
# Preview only: downloads/verifies/parses and writes local artifacts; no DB connection.
uv run import-recipes --output data/recipe-preview
```

Optionally supply `--csv /path/to/recipes.csv` (same checksum required) and
`--vocab /path/to/vocab.json`. Use identical options for preview and import; the vocabulary
checksum is part of the import identity. Inspect report counts, quarantined rows, several
normalized records, unknown units and original quantities before continuing.

```bash
# Keep your existing .env. Copy .env.example only if .env is absent.
# DATABASE_URL must point to your intended local database/port.
docker compose up -d db --wait
uv run import-recipes --write --apply-schema --output data/recipe-import
```

`--apply-schema` applies pending packaged SQL migrations in order, with checksum tracking. It requires
`--write`; application startup never creates tables. Reuse it safely on subsequent runs.
A changed applied migration raises an error; future schema changes need new migrations
through the existing migration runner. Do not edit an applied SQL file.

Verify counts and idempotency:

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM recipes; SELECT id, report FROM recipe_imports;"'
uv run import-recipes --write --apply-schema --output data/recipe-reimport
# Repeat the SQL query: recipe count and import identity should be unchanged.
make dev
```

In a second terminal:

```bash
curl 'http://localhost:8000/api/v1/recipes?q=chicken&dataset_id=AkashPS11%2Frecipes_data_food.com&max_minutes=60&limit=5'
# Use an actual source_id returned above, preserving its leading zeros:
curl 'http://localhost:8000/api/v1/recipes/000038'
```

The optional repeated `ingredient` query parameter requires exact canonical ingredient
names. `max_minutes` excludes unknown times. Search does not certify dietary constraints
or scale portions. Search uses English PostgreSQL full-text ranking, not BM25.
Before import/migration, recipe routes return a controlled 503. Existing health endpoints
check infrastructure, not whether the corpus has been imported.

## Verification

Run `make check` for lint, formatting, strict types and tests. PostgreSQL tests
use disposable test databases and skip when the server is unavailable. The
successful local migration included full staging, idempotency, rollback and
backup-restore verification; see the retained evidence referenced by the
hybrid runbook. Local migration counts are not guarantees for future imports.
