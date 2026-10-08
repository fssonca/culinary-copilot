# H6 ranking evidence (2026-10-08, proposal only)

Status: AI-prepared evidence for Checkpoint D. The judgments in
`additional_judgments_v1.json` await owner review. Production ranking
(`recipes/repository.py`) is unchanged; the frozen Phase 1 results are
untouched. All database reads ran with `default_transaction_read_only=on`.

## Files

| File | What it is |
|---|---|
| `additional_judgments_v1.json` | 18 labels for hits that title-first newly surfaces, rubric `phase1-rubric-v1`, sha256 `2e79ee34bf7647a9739f499976a91cfa9e762118c72705b2594c840601924c52`. A separate layer: `checkpoint1_corrections.json` is never edited. |
| `compare.json` | 52 frozen Phase 1 cases, three rankings, scored against the recorded labels and against recorded plus the new layer. |
| `regression.json` | The 6 dish-versus-ingredient cases in `evals/cases/h6_dish_vs_ingredient_v1.json`. |

Reproduce from the repo root (the application database must be in the
same state; H5 re-ingestion would change results that contain the
malformed records):

```
uv run python scripts/retrieval_eval/h6_ranking_compare.py --extra-judgments evals/results/h6/additional_judgments_v1.json --out evals/results/h6/compare.json
uv run python scripts/retrieval_eval/h6_ranking_compare.py --cases evals/cases/h6_dish_vs_ingredient_v1.json --out evals/results/h6/regression.json
```

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

Title-first changes the top 5 of 14 cases; the boost changes 16. Inside
all top-5 slots, judged grade-2 hits fall from 42 to 36 (title-first) or
37 (boost), and grade-1 hits from 11 to 6 for both; the freed slots go to
unjudged recipes.

The two tables below score every ranking on the same 23 cases (those
where the current top 5 holds a judged hit). A top 5 without any judged
hit counts as 0, which is a lower bound for the experiments.

Against the recorded labels only:

| Ranking | Recall@5 (grade ≥1) | MRR@5 (≥1) | Recall@5 (grade 2) | MRR@5 (2) | Top 5 with no judged hit |
|---|---|---|---|---|---|
| current | 1.000 | 0.675 | 0.783 | 0.523 | 0 |
| title-first | 0.870 | 0.607 | 0.696 | 0.462 | 3 (DEV-18, DEV-19, HELD-17) |
| title boost | 0.870 | 0.664 | 0.696 | 0.491 | 3 (same cases) |

With the 18 AI-prepared judgments added:

| Ranking | Recall@5 (≥1) | MRR@5 (≥1) | Recall@5 (2) | MRR@5 (2) |
|---|---|---|---|---|
| current | 1.000 | 0.675 | 0.783 | 0.523 |
| title-first | 1.000 | 0.670 | 0.913 | 0.546 |
| title boost | 1.000 | 0.706 | 0.913 | 0.551 |

On the recorded labels alone, both experiments are worse than the
current ranking. They come out ahead only when the new judgments are
counted, so the case for either change rests on the owner's review of
those 18 labels.

`compare.json` also holds the per-ranking metrics that `run_baseline.py`
would report, where each ranking is scored only on the cases its own top
5 has judged hits for (23, 20 and 20 cases). Those show the boost ahead
even on recorded labels (MRR@5 0.764 against 0.675), but only because
the three cases it loses drop out of its denominator. They should not be
used to compare rankings.

Five recipes that only the boost surfaces have no judgment yet, so the
boost's expanded numbers count them as not relevant: DEV-03 (a corned
beef and cabbage soup), DEV-24 (pasta e fagioli), DEV-28 (a Bulgarian
soup), HELD-07 (a whole deboned chicken with rice) and HELD-09 (a
chocolate cake).

No malformed `summary` record (H5) appears in any frozen top 5, so these
results do not depend on the H5 re-ingestion.

## Dish versus ingredient regression cases

Six development cases: a dish request and an ingredient-phrase control
for adobo, tzatziki and hollandaise. They record evidence; they assert
nothing automatically. Adobo was found by screening single-word dish
queries for top-5 title hits; tzatziki and hollandaise by looking for
sauce-like dishes with the same collision.

| Dish case | current | title-first | title boost |
|---|---|---|---|
| adobo: titles naming the dish in the top 5 | 0 | 5 | 4 |
| tzatziki | 1 (rank 2) | 3 | 3 |
| hollandaise: rank of the sauce | 4 | 1 | 2 |

The three phrase controls ("chipotle peppers in adobo sauce", "chicken
gyro with tzatziki", "eggs benedict with hollandaise") keep the same top
5 under all rankings. A malformed `summary` record sits in the hollandaise
and benedict top 5s under every ranking.

## Recommendation (proposal; Checkpoint D decides)

Do not change production ranking yet. If the owner's review confirms most
of the 18 judgments, prefer the title boost over title-first: it matches
title-first on recall, ranks slightly better, and keeps ingredient
discovery requests unchanged. Before adopting it:

1. Review the 18 judgments, and judge the 5 boost-only recipes above.
2. Choose the weight on more than one case: 2.0 is the smallest of
   0.5, 1.0, 1.5, 2.0 and 4.0 that recovers adobo, and adobo is also a
   regression case, so it is tuned on its own test.
3. Decide whether free-text queries should be boosted at all. The agent's
   searches are free text, so in practice the boost would apply to every
   agent search, not only to dish requests.
4. Implement it in `recipes/repository.py` with harness and regression
   coverage, then re-run this comparison.

Risks: three frozen cases lose every judged hit under either experiment;
the boost changes more top 5s than title-first (16 against 14); and the
gain disappears if the owner downgrades a few of the new labels.
