# H6 ranking evidence (2026-10-08)

Status: evidence for Checkpoint D. **Owner decision (2026-10-08): keep
the current ranking** (option A in `docs/checkpoint-d-decisions.md`); a
title boost needs more evaluation after H8. Production ranking
(`recipes/repository.py`) is unchanged; the frozen Phase 1 results are
untouched. All database reads ran with `default_transaction_read_only=on`.

## Files

| File | What it is |
|---|---|
| `additional_judgments_v1.json` | 18 AI-prepared labels for recipes title-first newly surfaces, rubric `phase1-rubric-v1`, sha256 `2e79ee34…`. Superseded by v2; kept as history. |
| `additional_judgments_v2.json` | Owner-accepted at Checkpoint D: the 18 v1 grades unchanged (row 3 grade 1, the rest 2) with corrected explanations and DEV-25 equipment evidence, plus 5 recipes only the title boost surfaces (all grade 2; Pasta e Fagioli is not suitable for the vegan request). sha256 `3bfb09ff…`. |
| `compare.json` | First run (judgments v1). Kept as history; its metric keys say `recall`, see "Metric names". |
| `compare_v2.json` | Current run: recorded labels, recorded plus judgments v2, Hit@5 naming, pool coverage. |
| `regression.json` | The 6 dish-versus-ingredient cases in `evals/cases/h6_dish_vs_ingredient_v1.json` (no labels; also uses the old `recall` keys). |

The original labels in `evals/results/phase1/checkpoint1_corrections.json`
are never edited.

Reproduce from the repo root (the application database must be in the
same state; the H5 cleanup changes results that contain the malformed
records):

```
uv run python scripts/retrieval_eval/h6_ranking_compare.py --extra-judgments evals/results/h6/additional_judgments_v2.json --out evals/results/h6/compare_v2.json
uv run python scripts/retrieval_eval/h6_ranking_compare.py --cases evals/cases/h6_dish_vs_ingredient_v1.json --out evals/results/h6/regression.json
```

## Metric names

The metric first reported as "Recall@5" is **Hit@5** (also called
Success@5): 1 for a case when any judged-relevant recipe is in the top 5,
0 when the top 5 holds judged recipes but none is relevant. It does not
measure the share of all relevant recipes retrieved. The numbers are the
same; only the name was wrong. MRR@5 is the reciprocal rank of the first
judged-relevant recipe.

## Rankings compared

- **current**: the production repository functions.
- **title-first**: recipes whose title matches the query words come
  first, then the current order. Reconstructed from the 2026-10-07 owner
  note (the SQL was reverted); it reproduces that note's numbers exactly.
- **title boost**: the current score plus 2.0 times the title's own
  full-text score. A title match raises a recipe but does not override
  the body score. It applies to dish-mode requests and to every
  free-text query; only pantry-overlap (ingredient discovery) requests
  keep the current order. Free text has no dish-intent signal, so phrase
  queries are boosted too.

## Results on the 52 frozen cases

Title-first changes the top 5 of 14 cases; the boost changes 16 (17
cases change under at least one). Inside all top-5 slots, judged grade-2
hits fall from 42 to 36 (title-first) or 37 (boost), and grade-1 hits
from 11 to 6 for both; the freed slots go to recipes the original labels
never judged.

**Judgment coverage.** The pooled top 5s of the three rankings hold 111
query-recipe pairs in the 17 changed cases. The original labels leave 68
of them unjudged; with judgments v2, 45 remain unjudged. Across all 52
cases, 105 of 187 pairs remain unjudged. Unjudged pairs are unknown, not
irrelevant, so every number below is from an incomplete pool.

The tables score every ranking on the same 23 cases: those where the
current top 5 holds a judged recipe. That set is chosen using the current
ranking, so it is a paired diagnostic, not a full-suite score (DEV-03,
DEV-28 and HELD-07, for example, stay outside it even with the new
labels). A top 5 with no judged recipe counts as 0.

Against the original labels only:

| Ranking | Hit@5 (grade ≥1) | MRR@5 (≥1) | Hit@5 (grade 2) | MRR@5 (2) | Top 5 with no judged recipe |
|---|---|---|---|---|---|
| current | 1.000 | 0.675 | 0.783 | 0.523 | 0 |
| title-first | 0.870 | 0.607 | 0.696 | 0.462 | 3 (DEV-18, DEV-19, HELD-17) |
| title boost | 0.870 | 0.664 | 0.696 | 0.491 | 3 (same cases) |

The three cases with no judged recipe are a finding against the original
labels: with judgments v2 each of them holds a judged-relevant recipe.

With judgments v2 added:

| Ranking | Hit@5 (≥1) | MRR@5 (≥1) | Hit@5 (2) | MRR@5 (2) |
|---|---|---|---|---|
| current | 1.000 | 0.675 | 0.783 | 0.523 |
| title-first | 1.000 | 0.670 | 0.913 | 0.546 |
| title boost | 1.000 | 0.706 | 0.913 | 0.562 |

On the original labels both experiments are below the current ranking;
with the new judgments both are above it, but 45 pairs in the changed
cases are still unjudged. `compare_v2.json` also holds the per-ranking
metrics `run_baseline.py` would report, where each ranking is scored only
on its own judged cases (23, 20 and 20); those should not be used to
compare rankings.

No malformed `summary` record (H5) appears in any frozen top 5.

## Dish versus ingredient regression cases

Six development cases: a dish request and an ingredient-phrase control
for adobo, tzatziki and hollandaise. They record evidence; they assert
nothing automatically.

| Dish case | current | title-first | title boost |
|---|---|---|---|
| adobo: titles naming the dish in the top 5 | 0 | 5 | 4 |
| tzatziki | 1 (rank 2) | 3 | 3 |
| hollandaise: rank of the sauce | 4 | 1 | 2 |

The three phrase controls keep the same top 5 under all rankings. A
malformed `summary` record sits in the hollandaise and benedict top 5s.
The boost weight (2.0) was chosen on adobo, so the adobo result is a
regression check, not independent evidence.

## What a future ranking change needs

From the Checkpoint D review:

1. An explicit search intent (dish, ingredient or unknown), with the
   current ranking for unknown intent, instead of boosting all free text.
2. Complete pooled top-5 judgments on the development cases before
   choosing, with held-out results reported separately.
3. A predetermined case set with judgment coverage reported.
4. The weight chosen on development cases and confirmed on fresh unseen
   cases; suitable-result metrics reported alongside topical ones.
5. One explicit rule for broad single-ingredient queries ("garlic"),
   applied to old and new labels alike.
