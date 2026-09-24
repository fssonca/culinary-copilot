# Corrected full-text baseline — post Food.com rebuild (read-only measurement)

> AI-assisted review accepted by the project owner; not independently human-verified or culinarily validated. Measurements against recorded labels only; not production reliability or culinary safety certification.

## Scoring defect and correction

- Defect: the superseded report scored only the 23 cases with a judged production top-5 hit, dropping eligible cases with zero relevant retrieved. Corrected eligibility comes from frozen reference judgments; misses score 0 and stay in the denominator.
- Correction (`scripts/retrieval_eval/rescore_baseline.py`, pure functions unit-tested in `tests/test_retrieval_metrics.py`): Recall@5 = relevant retrieved in top-5 / all labeled relevant; MRR@5 = reciprocal rank of first relevant or zero; identity matching on exact (dataset_id, source_id) pairs. Retrieval outputs reused from the saved post-rebuild run; no retrieval re-run, no tuning, no label changes.

## Threshold history (when and why the cutoff changed)

- Frozen declaration (`checkpoint1_corrections.json` `metrics_intended`): topical Recall@5/MRR at **topical_grade 2**, joint as grade-2 AND suitable. The week-2 plan requires 'a declared relevance cutoff'; this is it.
- No prior decision changed it: the first corrected issue used grade >= 1 without authorization. That broad-cutoff number is retained below **only as a sensitivity analysis**.
- Primary baseline therefore uses **strict relevance (grade 2)**; rubric grade 1 (same dish family, e.g. Cioppino for bouillabaisse) counts as relevant only in the sensitivity analysis.

## Primary baseline — strict relevance (grade 2, frozen declared cutoff)

- Recall@5: **0.408** over **27 relevant-labeled units**.
- MRR@5: **0.446** over the same units.
- Judged-only-negative pools (labels exist, none at grade 2): ['DEV-14', 'DEV-18', 'DEV-19', 'DEV-26', 'DEV-27'].
- Insufficient judgments: 20 cases (excluded, never zero-graded).

### Joint suitability at grade 2 (levels stated)

- Frozen label profile over 134 grade-2 case/recipe judgments (retrieval-independent): suitable 86 / unknown 28 / unsuitable 20 / unjudged 0.
- Retrieval joint recall@5: **29/86** suitable grade-2 case/recipe pairs retrieved in their case top-5.
- not_applicable handling: such constraints never fail a case; with no applicable constraint the joint verdict falls back to topical acceptability, so recorded suitable/not_applicable both resolve suitable while unknown stays unknown.

### By split, grade 2 (held-out must not drive tuning)

- development: recall@5 0.403 (n=16).
- held_out: recall@5 0.415 (n=11).

## Sensitivity analysis — broad relevance (grade >= 1, NOT the baseline)

- Recall@5: **0.400** over **30 units**; MRR@5: **0.518**.
- Frozen profile over 162 labels: suitable 106 / unknown 36 / unsuitable 20; retrieval joint recall **40/106**.
- Judged-only-negative: ['DEV-14', 'DEV-26']; insufficient: 20 cases.
- The prior case-level joint counts (20/3/0 over 23) are superseded by both sections above; they shared the selection bias.

## Eligibility partition (52 frozen cases; strict threshold)

- Relevant-labeled (scored): 27 units.
- Independently established no-match: none. Expected-empty intent cases (DEV-21/22/23, HELD-10/11/20) are listed, not verified; all sit in insufficient_judgments. No corpus absence is inferred from any empty pool or result.
- 'No quarantined cases' is distinct from the above metric exclusions.

## Coverage limitation (unchanged from saved outputs)

- Production top-5 hits: 56 judged / 105 unjudged. Recall measures against the labeled pool, not exhaustive corpus relevance. Same frozen 52-case cohort is preserved for later retriever comparisons (`run_baseline.py` + `rescore_baseline.py`).
- Latency, violations (0), and environment details: see the superseded report's saved-output sections, reused unchanged.

## Per-case numerator/denominator table (strict, grade 2)

| unit | split | numerator | denominator | recall@5 | mrr@5 |
|---|---|---|---|---|---|
| case:DEV-01 | development | 1 | 3 | 0.333 | 0.250 |
| case:DEV-05 | development | 1 | 1 | 1.000 | 0.250 |
| case:DEV-06 | development | 1 | 6 | 0.167 | 1.000 |
| case:DEV-07 | development | 0 | 7 | 0.000 | 0.000 |
| case:DEV-08 | development | 0 | 5 | 0.000 | 0.000 |
| case:DEV-09 | development | 1 | 3 | 0.333 | 0.500 |
| case:DEV-10 | development | 0 | 3 | 0.000 | 0.000 |
| case:DEV-11 | development | 1 | 1 | 1.000 | 0.500 |
| case:DEV-12 | development | 3 | 4 | 0.750 | 0.500 |
| case:DEV-13 | development | 1 | 1 | 1.000 | 1.000 |
| case:DEV-15 | development | 4 | 7 | 0.571 | 1.000 |
| case:DEV-20 | development | 0 | 3 | 0.000 | 0.000 |
| case:DEV-24 | development | 5 | 12 | 0.417 | 1.000 |
| case:DEV-25 | development | 3 | 8 | 0.375 | 1.000 |
| case:DEV-28 | development | 0 | 2 | 0.000 | 0.000 |
| case:DEV-29 | development | 5 | 10 | 0.500 | 1.000 |
| case:HELD-02 | held_out | 0 | 6 | 0.000 | 0.000 |
| case:HELD-03 | held_out | 2 | 4 | 0.500 | 0.250 |
| case:HELD-04 | held_out | 0 | 6 | 0.000 | 0.000 |
| case:HELD-05 | held_out | 0 | 3 | 0.000 | 0.000 |
| case:HELD-06 | held_out | 1 | 1 | 1.000 | 1.000 |
| case:HELD-09 | held_out | 1 | 1 | 1.000 | 0.333 |
| case:HELD-14 | held_out | 5 | 10 | 0.500 | 1.000 |
| case:HELD-15 | held_out | 0 | 7 | 0.000 | 0.000 |
| case:HELD-16 | held_out | 5 | 12 | 0.417 | 1.000 |
| case:HELD-17 | held_out | 1 | 1 | 1.000 | 0.250 |
| case:HELD-19 | held_out | 1 | 7 | 0.143 | 0.200 |

## Per-case table (sensitivity, grade >= 1)

| unit | split | numerator | denominator | recall@5 | mrr@5 |
|---|---|---|---|---|---|
| case:DEV-01 | development | 1 | 3 | 0.333 | 0.250 |
| case:DEV-05 | development | 1 | 1 | 1.000 | 0.250 |
| case:DEV-06 | development | 1 | 6 | 0.167 | 1.000 |
| case:DEV-07 | development | 0 | 7 | 0.000 | 0.000 |
| case:DEV-08 | development | 0 | 5 | 0.000 | 0.000 |
| case:DEV-09 | development | 1 | 7 | 0.143 | 0.500 |
| case:DEV-10 | development | 0 | 3 | 0.000 | 0.000 |
| case:DEV-11 | development | 2 | 2 | 1.000 | 1.000 |
| case:DEV-12 | development | 3 | 4 | 0.750 | 0.500 |
| case:DEV-13 | development | 2 | 4 | 0.500 | 1.000 |
| case:DEV-15 | development | 5 | 10 | 0.500 | 1.000 |
| case:DEV-18 | development | 2 | 4 | 0.500 | 0.333 |
| case:DEV-19 | development | 1 | 2 | 0.500 | 0.250 |
| case:DEV-20 | development | 0 | 3 | 0.000 | 0.000 |
| case:DEV-24 | development | 5 | 12 | 0.417 | 1.000 |
| case:DEV-25 | development | 3 | 9 | 0.333 | 1.000 |
| case:DEV-27 | development | 1 | 2 | 0.500 | 0.250 |
| case:DEV-28 | development | 0 | 2 | 0.000 | 0.000 |
| case:DEV-29 | development | 5 | 10 | 0.500 | 1.000 |
| case:HELD-02 | held_out | 0 | 6 | 0.000 | 0.000 |
| case:HELD-03 | held_out | 2 | 4 | 0.500 | 0.250 |
| case:HELD-04 | held_out | 1 | 7 | 0.143 | 0.500 |
| case:HELD-05 | held_out | 1 | 7 | 0.143 | 1.000 |
| case:HELD-06 | held_out | 1 | 1 | 1.000 | 1.000 |
| case:HELD-09 | held_out | 3 | 3 | 1.000 | 1.000 |
| case:HELD-14 | held_out | 5 | 10 | 0.500 | 1.000 |
| case:HELD-15 | held_out | 0 | 8 | 0.000 | 0.000 |
| case:HELD-16 | held_out | 5 | 12 | 0.417 | 1.000 |
| case:HELD-17 | held_out | 1 | 1 | 1.000 | 0.250 |
| case:HELD-19 | held_out | 1 | 7 | 0.143 | 0.200 |
