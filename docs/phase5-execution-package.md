# Phase 5 execution package (corrected — prepared, NOT authorized)

> **Execution record (2026-09-25):** the owner approved §§5–6 in full and
> every step below was executed in order. Backup verified restorable
> (16,033 recipes / 443 quarantine / 001–003, identical sample rows);
> container recreated on digest `sha256:724a...` (healthy); post-swap
> identity and collation green (2.41 == 2.41, 0 warnings); `004` applied via
> `scripts/migrate.py` (`applied=['004'] skipped=0`); pgvector 0.8.6
> installed; counts unchanged; full-text + exact lookup verified
> (`Chicken Biryani`); live embedding job completed — 16,033 chunks,
> 17,581,940 tokens used, **$0.3516 estimated spend** vs $0.7033 reserved
> (under the $2.00 ceiling); 16,033 vectors stored, 0 length violations;
> vector self-query returns 5 rows with top distance 0.0. No API container
> exists in this environment, so API stop/restart steps were vacuous
> (nothing stopped, nothing started). `.env` left untouched (ceiling passed
> as `--ceiling-usd 2.00`). Phase 6 comparison NOT run (needs its own
> authorization).

No application DB/container changes or paid calls were made in this pass.
The pinned trixie image was pulled once, as authorized. Everything below is
reviewable; stop before the application backup, image swap, migration 004
and live embedding job. Requires the owner's final checkpoint-3 approval
attaching model, migration, and dollar authorization.

Owner decisions (checkpoint 3, recorded 2026-09-24): embedding model
`text-embedding-3-small` 1536 dims only (`gpt-6-luna` stays the only
text-generation model; registries separate and mutually refusing —
verified); `$2.00` ceiling against the $0.70 reservation (recomputed with
the byte bound: 35,163,880 tokens retry-x2 = $0.7033 — fits); pinned image
`pgvector/pgvector:pg17-trixie@sha256:724a4041afdb1750446e3f6b5cfa8f3b0ac5a2cf538ddfa6bfee4f94c2fa85c6`.

## 1. Application target identity (observed read-only, 2026-09-25)

- Compose project `culinary-copilot`, container `culinary-copilot-db-1`
  (running since 2026-09-21), image `postgres:17` digest
  `sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675`.
- Volume `culinary-copilot_backend_postgres_data` →
  `/var/lib/docker/volumes/culinary-copilot_backend_postgres_data/_data`.
- PostgreSQL 17.11; OS Debian 13 trixie; glibc 2.41; `datcollate`
  `en_US.utf8`, `datcollversion` 2.41, actual collation version 2.41, no
  collation-version warnings.
- Database size 151 MB; recipes 16,033 (`AkashPS11/recipes_data_food.com`
  1216, `odunola/foodie` 14817); `recipe_quarantine` 443 rows (12
  quarantined, 431 rejected); `recipe_imports` 2 rows
  (`6beae552913554cc4c10a72d7ee41557d3717668c7b2d7d6e817e621c0b4ca34`,
  `foodie-repaired-v4-20260923`).
- `recipe_schema_migrations`: 001, 002, 003 only — **migration 004 is still
  unapplied to the application database**.
- `pg_available_extensions` has no `vector` row; no `vector` extension
  installed.

## 2. Collation compatibility

The pinned trixie image reports glibc 2.41 — identical to the running
database's glibc 2.41 and collation version 2.41 — so the image swap needs
no REINDEX. The earlier bookworm image (glibc 2.36) would have risked a
collation-version mismatch with associated index-corruption warnings.
Post-swap check (in the command list below): `datcollversion` against
`pg_database_collation_actual_version()`, plus a check that no
collation-version warnings appear; rehearsal on the swap volume showed
2.41 == 2.41 with zero warnings.

## 3. Backup (application commands prepared, NOT run)

```sh
# Logical backup of the application database + globals (run day, before anything else).
docker exec culinary-copilot-db-1 pg_dump -U copilot -d culinary_copilot -Fc -f /tmp/culinary-copilot-pre-pgvector.dump
docker cp culinary-copilot-db-1:/tmp/culinary-copilot-pre-pgvector.dump backups/culinary-copilot-pre-pgvector.dump
docker exec culinary-copilot-db-1 pg_dumpall -U copilot --globals-only -f /tmp/culinary-globals.sql
docker cp culinary-copilot-db-1:/tmp/culinary-globals.sql backups/culinary-globals.sql
sha256sum backups/culinary-copilot-pre-pgvector.dump backups/culinary-globals.sql | tee backups/culinary-copilot-pre-pgvector.sha256
# Restore verification into a disposable database on the pinned trixie image;
# compare row counts, migration versions, and exact-identity samples (see §7).
```

Optionally, a stopped-database volume snapshot (extra safety, costs API
downtime plus ~151 MB disk):

```sh
docker compose stop api db
docker run --rm -v culinary-copilot_backend_postgres_data:/data -v "$PWD/backups:/backup" alpine tar czf /backup/pgdata-pre-pgvector.tgz -C /data .
docker compose start db
```

Rehearsed end to end on disposable infrastructure: `pg_dump -Fc` +
`pg_dumpall --globals-only` on trixie, restore into `trix_restore`,
verified 25 recipes / 25 embeddings / 001–004 with identical sample rows
and embedding hashes (§7).

## 4. Migration-only CLI

`scripts/migrate.py` applies pending migrations and nothing else (unlike
`import_data --write --apply-schema`, which would re-import and replace the
Food.com snapshot). `--dry-run` lists pending versions plus extension
availability with zero writes (not even the migrations table is created —
tested). Without `--dry-run` it refuses unless the target matches
`--expect-db-name`/`--expect-db-host` (tested, exit 2). Vector skips print
their reason via the new `apply_migrations(..., listener=...)` hook
(tested on stock: 001–003 applied, 004 skipped with reason; on trixie: 004
applied, zero skips).

## 5. Exact application commands (prepared, NOT run)

Each step states its expected output and stop condition. Stop on any
mismatch and recover (§8) before proceeding.

```sh
# 1. Pre-checks (expected: 16033 recipes, 443 quarantine, 001-003, no vector).
uv run python -c "from sqlalchemy import create_engine,text; e=create_engine('postgresql+psycopg://copilot:copilot_dev@localhost:5432/culinary_copilot'); c=e.connect(); print(c.execute(text('SELECT count(*) FROM recipes')).scalar_one(), c.execute(text('SELECT version FROM recipe_schema_migrations ORDER BY 1')).scalars().all())"
# Stop unless: 16033, ['001','002','003'].
# 2. Backup + checksum (§3). Stop unless sha256 verifies.
# 3. Stop the API (DB keeps running): docker compose stop api
# 4. Image swap on the same volume (override now pins the digest):
docker compose -f compose.yaml -f compose.pgvector.override.yaml up -d db
# Stop unless the db container image digest is sha256:724a... (docker inspect).
# 5. Health: pg_isready via compose healthcheck; stop unless healthy.
# 6. Post-swap identity + collation: datcollversion == actual == 2.41, no
#    collation warnings, 16033 recipes. Stop on any drift.
# 7. Migration dry-run (zero writes), then apply:
uv run python scripts/migrate.py --dry-run   # expect 004 PENDING — will apply
uv run python scripts/migrate.py --expect-db-name culinary_copilot --expect-db-host db
# Stop unless: applied=['004'] skipped=0.
# 8. Verification: 004 recorded, vector extension present, recipe/quarantine
#    counts unchanged (16033 / 443), fixed-sample full-text + exact lookup
#    unchanged, pending_vector_migrations() empty.
# 9. Restart the API: docker compose up -d api; stop unless /health/ready passes.
```

## 6. Embedding job commands (prepared, NOT run)

```sh
# Fresh reservation (zero network, zero writes); stop unless it fits $2.00.
uv run python scripts/embeddings/embed.py --run-dir data/embeddings/prod-run --dry-run --ceiling-usd 2.00
# Live job (requires OPENAI_API_KEY + EMBED_BUDGET_USD=2.00 in .env):
uv run python scripts/embeddings/embed.py --run-dir data/embeddings/prod-run --live --yes --ceiling-usd 2.00
# Resume is idempotent (re-run embeds only missing/changed chunks; verified).
# Record: ledger used_tokens_total vs reservation, provider usage totals,
# estimated spend = tokens/1e6 × $0.02. Stop on ceiling breach, integrity
# failure, or any config outside this approval.
```

## 7. Rehearsal results on the pinned trixie image (2026-09-25)

Pulled image verified: digest `sha256:724a4041...` (exact match),
Debian 13 trixie, glibc 2.41, PostgreSQL 17.11, pgvector 0.8.6.

- **Swap simulation**: disposable volume `pg5-swap-vol` created on stock
  `postgres:17` (001–003 via migrate CLI, 004 skipped with reason, 25
  recipes seeded); container stopped; pinned trixie image started on the
  same volume — 25 recipes intact, collation 2.41 == actual 2.41, no
  warnings. Migrate CLI dry-run then apply: `applied=['004'] skipped=0`.
- **Fake job**: 25 chunks embedded; resume run embedded 0 chunks, 0
  requests; real 64-char fingerprints in ledger.
- **Stale exclusion**: renderer-`0` chunk-99 row stays in the table but the
  recipe counts 1 chunk — old versions never score. `vector_dims` CHECK
  verified: 3072-dim row accepted, 999-dim row rejected, label/vector
  mismatch (1536 label, 3-element vector) rejected, correct 1536 row
  accepted (all in disposable-DB tests).
- **Source change**: title edit re-queued exactly 1 chunk with the new hash.
- **Crash recovery**: deleted ledger entry re-embedded exactly 1 chunk, PK
  upsert, still 25 current rows.
- **Backup/restore**: `pg_dump -Fc` + globals dump, restore into
  `trix_restore`: 25 recipes, 25 embeddings, 001–004, identical sample rows
  and embedding hashes.
- **Revert**: post-004 revert SQL (`DROP TABLE recipe_embeddings,
  embedding_runs; DROP EXTENSION vector; DELETE FROM
  recipe_schema_migrations WHERE version='004'`) makes 004 pending again;
  image reverted to stock `postgres:17` on the same volume — 25 recipes,
  001–003, no vector objects, full-text + exact lookup (`Chimodho`)
  verified.
- **Test tier**: `PGVECTOR_TEST_URL` → trixie: **26 passed** (migration CLI,
  pantry boost, dimension CHECKs, resume, crash recovery, ledger guard,
  hybrid plumbing). Stock tier: 004-skip + full-text intact.

## 8. Recovery, stated honestly

- Before 004 is applied: revert the override and recreate the container on
  `postgres:17` with the same volume (`docker compose up -d db` without the
  override file). Nothing in user data changes.
- After 004 is applied: `vector` objects exist that the stock image cannot
  load, so the image must not be reverted first. Either run the exact
  revert SQL from §7 (this makes 004 pending again) and then revert the
  image, or restore the full verified backup from §3.
- SQL rollback alone cannot undo the container or image change; both paths
  were rehearsed on the disposable volume, not merely described.

## 9. What is implemented, mocked, rehearsed, unexecuted

- **Implemented**: embedding registry/provider/rendering/query,
  `OpenAIEmbeddingProvider`, byte-bound reservations, atomic per-recipe
  replacement with cascading deletes, `vector_dims` + dimension CHECKs,
  vector + pantry-boosted RRF search, `retrieve_for_group(..., mode=...)`
  (full-text default, zero calls), resumable CLI with disposable-DB and
  pid guards, migration-only CLI with dry-run/guard/skip reasons,
  digest-pinned pgvector override (not applied).
- **Mocked**: live provider responses in unit tests — no network touched.
- **Rehearsed**: everything in §7 on the pinned image.
- **Unexecuted**: application backup, image swap, migration 004, live
  embedding job ($0.00 spent), Phase 6 comparison. Fake vectors prove
  plumbing only, never relevance.

## Historical note (superseded bookworm rehearsal)

An earlier rehearsal ran `pgvector/pgvector:pg17` (bookworm, glibc 2.36)
with char-based reservations ($0.70/35,145,866 tokens). It is superseded by
the trixie run above: byte-bound reservation $0.7033/35,163,880 tokens,
pinned digest, full swap + revert. The bookworm image must not be used for
the application (collation risk).

## Held-out status (corrected)

The existing 30/20 set was exposed during earlier reviews and is NOT
sealed/blind. Keep it frozen with its exposure log; preserve grade-2
primary relevance, fixed denominators, explicit unjudged-result treatment.
Reserve any fresh blind set for later confirmation.
