# jojogo9/Food_Recipes — provenance and coverage report (Workstream 3)

Read-only audit. No bulk import was performed. Raw audit JSON lives in the
ignored directory `data/jojogo9-audit/audit-report.json`; nothing under
`data/` is published or committed.

## A. Provenance and licensing

### Verified facts (checked 2026-09-22)

- Repo: <https://huggingface.co/datasets/jojogo9/Food_Recipes>
- Revision (main): `5238aa9ec900667b205e465a78eedb36c4ffdb79`;
  last modified `2024-04-23T03:25:51Z`.
- Files: `.gitattributes` (2,307 B) + `food_recipes.parquet` (132,121,597 B,
  LFS SHA-256 `22dd0e58910700b01e8cfd74a9c1ff438d1dbb4f39e00d60db06bc2041133182`).
- No dataset card, no README, no LICENSE file: `cardData=None`, all
  license/description/citation/homepage fields empty.
- Columns (12): `name` (string), `id` (int64), `minutes` (int64),
  `contributor_id` (int64), `submitted` (string `YYYY-MM-DD`), `tags`,
  `nutrition`, `steps`, `ingredients` (all four stored as **strings**
  holding Python-literal lists, not native list columns), `description`
  (string), `n_steps` (int64), `n_ingredients` (int64).
- Rows: **231,637** (pyarrow metadata, 232 row groups); all IDs unique.
- Related Food.com sources inspected for comparison:
  - `AkashPS11/recipes_data_food.com` (our current corpus): revision
    `aa68f5bf9c9f33a9fe4624e180d9e80a2030c675`, 1,048,543 CSV rows, 29
    columns, absolute nutrition columns, card declares MIT with an
    effectively empty README (21 B). Verified 2026-09-22.
  - `tiptoghosh/food-recipes-15k`: revision
    `b3f8de1059f94d1329042a8817454e4e148e6fbd`, 15,698 rows, labeled
    `nutrition` struct
    `{calories, total_fat_pdv, sugar_pdv, sodium_pdv, protein_pdv,
    saturated_fat_pdv, carbs_pdv}` (float32), license **apache-2.0**.
    Its README section “Nutrition Values — Important Note” states values
    come from Food.com as **Percent Daily Values except Calories (kcal)**,
    per serving on a 2,000-calorie diet. Verified 2026-09-22 at
    <https://huggingface.co/datasets/tiptoghosh/food-recipes-15k/raw/main/README.md>.
- Kaggle upstream candidate
  <https://www.kaggle.com/datasets/shuyangli94/food-com-recipes-and-user-interactions>:
  page is JS-rendered; only the title was retrievable. Contents **unverified**
  directly. Its `RAW_recipes.csv` is cited by tiptoghosh as the source with
  the same 12-column layout (indirect evidence only).

### Plausible but unverified

- jojogo9 is most likely a Parquet republish of the Kaggle
  `RAW_recipes.csv` (identical 12 columns, stringified-list encoding,
  231,637-row scale, Food.com ID range). No in-repo attribution confirms this.

### Missing evidence

- Any license or reuse permission for jojogo9 itself.
- Any in-repo source references, homepage, citation, or upstream links.
- Any in-repo documentation of the 7-element nutrition positions.
- Direct verification of the Kaggle page contents.

Repository license declarations are recorded separately from upstream reuse
permissions: jojogo9 declares **nothing**; tiptoghosh declares apache-2.0
for its derivative (which does not transfer to jojogo9); AkashPS11 declares
MIT with minimal provenance. Public download availability implies no
permission. Admission stays **pending** on licensing (see recommendation).

## B. Structure audit (safe parsers, bounded)

Parser: bounded `ast.literal_eval` (200 kB field cap, 500-element cap,
expected element types); never `eval`; never executes dataset code.

| Check | Scope | Result |
| --- | --- | --- |
| Row count | full file | 231,637 |
| Unique IDs | full file | 231,637 (0 duplicates) |
| Missing titles | full file | 1 |
| Missing raw ingredients / steps | full file | 0 / 0 |
| `minutes` negative | full file | 0 |
| `minutes` zero | full file | 1,094 |
| `minutes` > 24 h | full file | 2,000 |
| `minutes` > 7 d | full file | 256 |
| `minutes` max | full file | **2,147,483,647** (`INT_MAX`, data error) |
| List-parse problems (tags/nutrition/steps/ingredients) | sample 2,000 (seed 7) | **none** |
| Nutrition list length | sample 2,000 | all **7** |
| `n_steps` / `n_ingredients` mismatches | sample 2,000 | **0 / 0** |
| Empty parsed lists | sample 2,000 | none |
| Duplicate content fingerprints | sample 2,000 | 0 |
| Quantities | schema | **absent** — ingredients are bare names |
| Servings | schema | **absent** — no servings column |
| Nutrition labels/units | schema | **absent** — bare 7-float lists |

Retain source-reported durations as-is: the `INT_MAX` minutes value and
multi-day values are reported, never reinterpreted as active cooking time.

## C. Overlap and incremental coverage

Method: full-file integer-ID overlap plus sampled content fingerprints
(normalized title + ingredient names + ordered steps). The Food.com
zero-padded ID `000038` was compared as integer `38` through a dedicated
comparison key; no global zero-stripping was applied to any dataset.
Title equality alone is never treated as duplication. Corpus fingerprints
come from `data/recipe-preview/normalized.jsonl` (v2; v3 uses identical
hash inputs, so comparison is valid).

| Finding | Value |
| --- | --- |
| Current corpus size | 1,216 recipes |
| jojogo9 IDs also present in current corpus (full file) | **700 of 1,216** |
| Exact content-fingerprint matches in 2,000-sample | **0** |
| Title-only matches needing review in sample | 7 |
| Matches against odunola pilot (144) in sample | 0 |

Worked example — Food.com ID 38: present in both (`000038` ↔ `38`).
Same title and same 4 ingredient names, but **13 steps in jojogo9 vs 9 in
our corpus** (different sentence segmentation/casing) and different
nutrition encodings (jojogo9 list `[170.9, 3.0, 120.0, …]` vs our absolute
columns; first elements agree at 170.9, consistent with kcal-first). This is
a **conflicting version of the same apparent recipe**, not independent
corroboration: same upstream recipe, re-serialized differently.

Coverage assessment:

- Additional ingredient/tag breadth: large in absolute terms (231k records,
  tags present throughout) but limited per record (bare names, no
  quantities, no servings, PDV-only nutrition with unverified positions).
- Complete-recipe evidence: **not improved** — no quantities, servings, or
  validated units; step segmentation conflicts with our corpus on the
  checked example.
- Discovery breadth: potentially large (231k − overlap), but unmatched rows
  are **not** labeled “unique recipes”: deduplication so far is exact
  fingerprints on a 2,000-sample plus ID overlap; near-duplicate analysis
  at full-file scale was not performed.
- The 700 ID overlaps mean over half our current corpus would collide on
  upstream identity with a conflicting serialization — a pilot must resolve
  version precedence before any write.

## D. Admission recommendation: HOLD

**Hold pending provenance/license clarification. Prefer a
better-documented upstream source.**

- *Measured coverage*: 231,637 rows, clean serialization (0 sampled parse
  failures), but per-record completeness is lower than our corpus for
  generation purposes (no quantities/servings; nutrition positions
  externally inferred, not self-documented).
- *Data completeness*: durations present with anomalies (`INT_MAX`);
  quantities/servings structurally absent.
- *Provenance evidence*: no license, no card, no attribution; upstream link
  plausible but unverified; permission cannot be inferred from
  downloadability.
- If a Food.com-scale corpus is later needed, prefer `tiptoghosh/food-recipes-15k`
  (apache-2.0, labeled nutrition struct, documented PDV semantics) or the
  Kaggle upstream after direct verification — not this mirror.
- A bounded pilot (≤200 records, isolated DB, version-precedence rules for
  the 700 colliding IDs) may proceed **only after** license clarity; the
  read-only technical audit above is complete and needs no further data
  access.

## Reproduce

```bash
uv run --with pyarrow python scripts/datasets/jojogo9_audit.py
uv run --with pyarrow python scripts/datasets/jojogo9_audit.py --sample 2000 --seed 7
```
