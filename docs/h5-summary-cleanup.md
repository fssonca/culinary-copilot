# H5 summary-layout cleanup: procedure and rehearsal

Approved in principle at Checkpoint D (2026-10-08, see
`docs/phase7-owner-decisions.md`), subject to a corrected, rehearsed
procedure. This document is that procedure. **The application run has
not happened; it needs the owner's go-ahead.** It supersedes the local
draft `data/h5-repair-preview/REINGESTION-PROCEDURE.md`.

## What it does

In one transaction, against one explicitly named database:

- deletes the 158 `odunola/foodie` recipes titled `summary` (their 158
  embedding rows go with them through the cascading foreign key);
- adds import `foodie-repair-v5-20261008` and 201 quarantine rows with
  reason `summary_layout_missing_title`: the 158, the 38 rows the
  2026-09-23 migration already quarantined, and the 5 exact duplicates
  whose canonical recipe is among the 158;
- leaves everything else unchanged, which it checks.

The 38 older quarantine rows stay as import history, so those source
rows appear under both imports: 201 unique source rows, 239 quarantine
events for them. Title recovery is deferred. No model calls, no new
embeddings.

## Tooling

- `scripts/datasets/h5_summary_cleanup.py` (tests:
  `tests/test_h5_summary_cleanup.py`):
  - `manifest` (read-only) freezes the condemned identities, the 201
    quarantine rows, the expected counts before and after, and
    fingerprints of every surviving recipe row, every other quarantine
    row and every other import row.
  - `apply` runs in one transaction. On the first run it checks the
    database matches the manifest, writes, deletes by identity (not by
    title) and checks the end state. On a rerun it finds the end state,
    verifies it and writes nothing. Any other state aborts. Every check
    raises and rolls the transaction back.
  - `verify` (read-only) checks the end state.
- The database is always passed with `--database-url`; nothing is taken
  from `.env` for the target. `--target rehearsal` refuses the
  application database and requires a name starting with
  `culinary_rehearsal`. `--target application` requires the application
  identity, `--confirm-application culinary_copilot`, and a backup file
  matching `--backup-sha256`.
- Quarantine rows go through `upsert_quarantine_rows` in
  `recipes/llm_batch.py`, the same insert `cmd_load` uses, now inside the
  cleanup's transaction.

## Inputs (local, under `data/h5-cleanup/`, not committed)

| File | sha256 (prefix) | What it is |
|---|---|---|
| `rows-201.txt` | `5a4687294b5e2770` | The 201 source rows, built read-only from the application database and the 2026-09-23 alias map. |
| `run/` (`manifest.json` `c0ee4f148580b393`, `final-quarantine.jsonl` `4570eb4daa0e3443`) | | `llm_batch prepare --rows … --limit 0 --audit-rate 0` then `finalize`, offline: 201 quarantined, 0 model requests, 0 loadable records, adapter v5, routing v3. |
| `manifest.json` | `7f96aef246f42ce9` | Frozen from the application database on 2026-10-08. |

Expected counts:

| | recipes | quarantine rows | imports | `summary` titles | foodie embedding rows |
|---|---:|---:|---:|---:|---:|
| before | 16,033 | 443 | 2 | 158 | 14,817 |
| after | 15,875 | 644 | 3 | 0 | 14,659 |

## Rehearsal (done 2026-10-08)

A read-only `pg_dump` of the application database
(`data/h5-cleanup/backup/rehearsal-source.dump`, mode 0600, sha256
`ba37c5f8ad8d116e…`) was restored twice into fresh disposable databases
(`culinary_rehearsal_h5_1`, `culinary_rehearsal_h5_2`) with
`pg_restore --exit-on-error`. In each:

- first `apply`: state `first_run`, 360 writes (158 deletes, 201
  quarantine rows, 1 import row), every end-state check passed;
- second `apply`: state `already_applied`, 0 writes;
- `verify`: passed.

In the first rehearsal database:

- `--target application` was refused ("application target does not
  match");
- searching "hollandaise" no longer returns a `summary` record, and the
  sauce moved from rank 4 to rank 3;
- legacy recipe `000038` is still present;
- foodie recipes matching "garlic" went from 4,694 to 4,667, exactly
  the 27 deleted `summary` records that matched.

Both rehearsal databases were dropped. The application database was
only read.

## Application run (after the owner's go-ahead)

1. Take a fresh backup and record its sha256; keep it outside Git,
   mode 0600:

   ```
   docker exec culinary-copilot-db-1 pg_dump -U copilot -d culinary_copilot -Fc > data/h5-cleanup/backup/application-before.dump
   chmod 600 data/h5-cleanup/backup/application-before.dump
   shasum -a 256 data/h5-cleanup/backup/application-before.dump
   ```

2. Restore it into a fresh `culinary_rehearsal_h5_verify` database and
   run `apply --target rehearsal` there once more, so the run is
   rehearsed on the exact backup. Drop that database.
3. Rebuild the manifest only if the application data changed since
   2026-10-08 (the apply step refuses a mismatch). Then:

   ```
   uv run python scripts/datasets/h5_summary_cleanup.py apply \
     --database-url "<application url>" \
     --run-dir data/h5-cleanup/run --manifest data/h5-cleanup/manifest.json \
     --target application --confirm-application culinary_copilot \
     --backup data/h5-cleanup/backup/application-before.dump --backup-sha256 <sha256> \
     --report data/h5-cleanup/application-report.json
   ```

4. Run `verify` against the application database, then re-run the H6
   comparison (its results include the malformed records) and record the
   new counts with the H8 freeze.

## Recovery

The run is one transaction: a failure leaves the application database as
it was. After a successful run, never restore over the live database
automatically: restore the step-1 backup into a new database, compare it
with the recorded counts, and switch over only by owner decision.

## Side effect already in the code

The H5 parser and routing version bump changes every foodie LLM cache
key, so cached extractions no longer match. Preparation and finalization
are offline; payment happens only on a later Batch submission. Before a
future full re-preparation, check which saved responses are still
compatible.
