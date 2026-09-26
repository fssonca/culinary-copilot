# Phase 6 retrieval scoreboard: frozen three-mode blind comparison

Blind adversarial confirmation set (20 requests), owner-judged. This file
holds aggregates, hashes and code pointers only — no recipe content. The
§7 decision winner is **vector_c** (cutoff 0.66); the default stays
**full-text** until the owner approves the flip separately.

## Configurations (frozen in `evals/results/phase6/freeze.json`)

Full-text is the frozen Phase 2 baseline, unchanged. Vector/hybrid use
`rrf_k = 60`, `vector_candidates_n = 20` (fixed, not tuned) with
`text-embedding-3-small`, 1536 dimensions, renderer version 1.
`(a)` no filter and `(b)` full-text gate are informational; `(c)` is the
0.66 cosine-distance cutoff chosen on development data.

| config | params | sha256 |
|---|---|---|
| fulltext | mode fulltext (Phase 2 baseline) | `51c05ede6329…` |
| vector_a | mode vector, no filter | `2b6e93e05e94…` |
| hybrid_a | mode hybrid, no filter | `59c0c1514350…` |
| hybrid_b | mode hybrid, full-text gate | `d82f651c5741…` |
| vector_c | mode vector, cutoff 0.66 | `f1b185dc7e5f…` |
| hybrid_c | mode hybrid, cutoff 0.66 | `717ab6310ac7…` |

(Full hashes in `freeze.json`.) Cutoff semantics: vector keeps distance ≤
cutoff and may abstain (`vector_cutoff_abstention`, never silent
full-text); hybrid applies it before fusion, keeping full-text as-is.
`allow_fallback = False` everywhere: an embedding failure is a recorded
error, never a silent result.

## Blind retrieval metrics (scored n=16)

Hit = ≥1 qualifying result (topic grade 2, no time/dataset violation) in
top 5. Scorer: `scripts/retrieval_eval/score_blind.py` (unit-tested);
outputs: `data/phase6/blind_scores.json` (ignored).

| mode | HitRate@5 | MRR@5 | Pooled Recall@5 (n=16) | Pooled Recall@5 (n=13 with pooled qualifying) | nDCG@5 | net wins vs FT |
|---|---|---|---|---|---|---|
| fulltext | 0.438 | 0.359 | 0.173 | 0.214 | 0.426 | — |
| vector_a (info) | 0.812 | 0.703 | 0.616 | 0.759 | 0.856 | +6 |
| vector_c | 0.812 | 0.703 | 0.616 | 0.759 | 0.856 | +6 |
| hybrid_a (info) | 0.812 | 0.656 | 0.527 | 0.649 | 0.793 | +6 |
| hybrid_b (info) | 0.562 | 0.484 | 0.277 | 0.341 | 0.538 | +2 |
| hybrid_c | 0.812 | 0.656 | 0.527 | 0.649 | 0.793 | +6 |

Recall convention: cases with no qualifying pooled candidate (BLIND-02,
BLIND-12, BLIND-19) count as 0 over n=16; the n=13 column restricts to
cases with at least one. Recall is always labelled "pooled".

Per-case hit/miss + nDCG (modes FT, Va, Vc, Ha, Hb, Hc):

| case | FT | Va | Vc | Ha | Hb | Hc |
|---|---|---|---|---|---|---|
| BLIND-01 | H/0.54 | H/0.80 | H/0.80 | H/0.61 | H/0.61 | H/0.61 |
| BLIND-02 | m/0.00 | m/0.63 | m/0.63 | m/0.63 | m/0.00 | m/0.63 |
| BLIND-04 | H/0.93 | H/1.00 | H/1.00 | H/1.00 | H/1.00 | H/1.00 |
| BLIND-05 | m/0.00 | H/0.89 | H/0.89 | H/0.89 | m/0.00 | H/0.89 |
| BLIND-07 | H/1.00 | H/1.00 | H/1.00 | H/1.00 | H/1.00 | H/1.00 |
| BLIND-08 | m/0.00 | H/1.00 | H/1.00 | H/1.00 | m/0.00 | H/1.00 |
| BLIND-09 | H/0.64 | H/1.00 | H/1.00 | H/1.00 | H/1.00 | H/1.00 |
| BLIND-10 | m/0.00 | H/0.97 | H/0.97 | H/0.50 | H/0.50 | H/0.50 |
| BLIND-11 | H/1.00 | H/0.89 | H/0.89 | H/1.00 | H/1.00 | H/1.00 |
| BLIND-12 | m/1.00 | m/1.00 | m/1.00 | m/1.00 | m/1.00 | m/1.00 |
| BLIND-13 | m/0.00 | H/1.00 | H/1.00 | H/1.00 | m/0.00 | H/1.00 |
| BLIND-14 | H/0.93 | H/1.00 | H/1.00 | H/0.93 | H/0.93 | H/0.93 |
| BLIND-16 | m/0.00 | H/0.56 | H/0.56 | H/0.56 | m/0.00 | H/0.56 |
| BLIND-17 | H/0.76 | H/1.00 | H/1.00 | H/0.93 | H/0.93 | H/0.93 |
| BLIND-18 | m/0.00 | H/0.96 | H/0.96 | H/0.63 | H/0.63 | H/0.63 |
| BLIND-19 | m/0.00 | m/0.00 | m/0.00 | m/0.00 | m/0.00 | m/0.00 |

Candidate wins vs FT (6–0 for both candidates): BLIND-05, 08, 10, 13,
16, 18. FT wins vs either candidate: none. BLIND-02's pool holds no
grade-2 (all modes miss); BLIND-12's pool is all grade-1 (miss
everywhere, nDCG 1.0); BLIND-19's pool is empty (miss everywhere).

## Abstention (n=4: BLIND-03, 06, 15, 20)

| mode | CleanAbstentionRate | FalsePositivesPerQuery | NearMissesPerQuery | results total |
|---|---|---|---|---|
| fulltext | 1.000 | 0.00 | 0.00 | 0 |
| vector_c | 1.000 | 0.00 | 0.00 | 0 |
| hybrid_c | 1.000 | 0.00 | 0.00 | 0 |
| vector_a (info) | 0.250 | 3.75 | 0.00 | 15 |
| hybrid_a (info) | 0.250 | 3.75 | 0.00 | 15 |
| hybrid_b (info) | 1.000 | 0.00 | 0.00 | 0 |

The cutoff carries abstention here: uncut vector tops returned 15
results across the four cases; at 0.66 all are removed (3 vector-(c)
abstentions with the reason code).

## Hard correctness (§7.2)

0 time violations and 0 dataset violations for every mode, over every
returned result (149 pooled candidates checked mechanically).

## Slices (descriptive)

Scored = 8 integration + 8 retrieval_only; abstention = 2 + 2.

| slice | mode | HitRate | MRR | nDCG |
|---|---|---|---|---|
| integration | fulltext | 0.625 | 0.562 | 0.659 |
| integration | vector_c | 0.750 | 0.750 | 0.856 |
| integration | hybrid_c | 0.750 | 0.750 | 0.812 |
| retrieval_only | fulltext | 0.250 | 0.156 | 0.193 |
| retrieval_only | vector_c | 0.875 | 0.656 | 0.856 |
| retrieval_only | hybrid_c | 0.875 | 0.562 | 0.774 |

Suitability (descriptive only, never a gate): 113 not_applicable, 31
unsuitable, 5 suitable, 0 unknown across the 149 judged.

## §7 decision

Both candidates pass every gate (net wins 6 ≥ 2; MRR/nDCG ≥ FT; zero
violations; abstention totals 0 ≤ 0). Tie-break: HitRate tied 0.812;
MRR 0.703 vs 0.656 decides for **vector_c**. The MRR gap rests on 2 of
16 requests (first-qualifying-rank 2-vs-4 on BLIND-01, 1-vs-2 on
BLIND-10). Winner: **vector_c**. The default is unchanged (full-text)
pending separate owner approval.

Judging sensitivity (recomputed, official scores unchanged): with the AI
first-pass grades, and with BLIND-18 read as vegetarian (its three
grade-1s counted as hits), both candidates still pass and vector_c
still wins the tie-break on MRR.

## Development results (labelled development, informational)

Cutoff 0.66 was selected on 39 scored development cases (32 DEV + 12
hard P6D, of which P6D-03 has no grade-2) with 20 non-food probes:
NonFoodAbstention 0.95 (19/20; only the rescue-dog probe keeps 3
vector results), hybrid-(c) HitRate@5 0.974, MRR@5 0.962 — the loosest
cutoff at the top hit rate among values with abstention ≥ 0.90. At the
chosen value the only hard-query removals vs no cutoff are two grade-0
candidates. Development HitRate@5 at the freeze: fulltext 0.821,
vector_a/c 0.974, hybrid_a/c 0.974, hybrid_b 0.821. Development labels
are a frozen owner-reviewed layer (173) plus an AI-assisted agent layer
(293, not owner-reviewed). The development label layer
(`data/phase6/phase6_dev_labels_v1.json`, hash in `freeze.json`) is
self-contained and records its own provenance (rubric, cutoff, labeller,
source, view seed and blinding). Held-out cases (HELD-01..20, exposed in
earlier reviews) were **not run** in Phase 6.

## Latency (measured, read-only harness)

DB medians over 3 reps after 2 warmups; embedding medians over 3 reps
per distinct query; fetch medians over 3 reps of pooled-doc fetch.

| run | component | p50 (ms) | p95 (ms) |
|---|---|---|---|
| blind (20 req) | query embedding (paid API) | 564.6 | 714.4 |
| blind | DB full-text / vector / hybrid | 1.4 / 62.8 / 64.5 | 4.6 / 64.6 / 69.4 |
| blind | full-document fetch | 4.9 | 11.4 |
| blind | end-to-end, no embedding: FT / vector / hybrid | 6.9 / 67.5 / 69.3 | 15.8 / 75.0 / 79.5 |
| blind | end-to-end with embedding: vector / hybrid | 628.5 / 630.0 | 724.7 / 727.3 |
| dev (64 req) | query embedding (paid API) | 644.3 | 893.2 |
| dev | DB full-text / vector / hybrid | 1.5 / 67.7 / 69.6 | 29.2 / 70.3 / 78.2 |
| dev | full-document fetch | 4.7 | 10.0 |
| dev | end-to-end, no embedding: FT / vector / hybrid | 6.4 / 72.5 / 74.3 | 37.1 / 79.2 / 88.1 |
| dev | end-to-end with embedding: vector / hybrid | 704.4 / 707.0 | 924.0 / 925.6 |

Vector DB time is an exact scan (no HNSW index). One paid embedding per
search dominates vector/hybrid latency.

## Spend

Phase 6 query embeddings (`text-embedding-3-small`, $0.02/1M input
re-verified on the official pricing page before the first call;
per-call reservation with the byte-bound estimate, $0.05 aggregate
ceiling enforced by the harness):

| run | reserved | actual | ceiling |
|---|---|---|---|
| development (64 cases) | $0.000325 | $0.000028 | $0.05 |
| blind (20 cases) | $0.000102 | $0.000010 | $0.05 |

Phase 5 live embedding spend (historical): $0.3516.

## Corpus integrity

16,033 recipes / 16,033 vectors / 443 quarantine rows before and after
every Phase 6 run (read-only; sample lookups confirmed). Migrations
001–004 applied; no new migrations; no application-DB writes.

## Hashes and provenance

- Request file `5a613b5d…2f3c6e71f`, owner key `2bcf7d44…27a8ed81`,
  judging template `2c89b3a1…45126a8` (all match freeze commitments).
- Converted blind cases `5603308c…`, converter `50555a72…` (re-run
  byte-identical).
- The blind run omitted `--cutoff`, so its embedded `derived_top5` used
  cutoff None; `(c)` configurations were re-derived offline from the
  recorded top-20 lists at 0.66 (`blind_derived.json`, `57e62511…`),
  with no re-retrieval. The packet pool already contained every `(c)`
  result (packet unchanged).
- `freeze.json`'s `dirty_tree` listing was stale (missing
  `analyze_phase6.py` and `build_phase6_labelling_view.py`); the owner
  holds a per-file code hash list taken before the blind handover.
- Owner judging clarifications: 10 grades changed from the AI first
  pass; BLIND-18 "meat-free" was read as vegan, stricter than the key's
  wording; external recipe pages were consulted, with each change
  checked against packet evidence.
- Blind raw output `eee25bbf…`, blind derived `57e62511…`,
  packet `84558453…`, blind scores `fa895676…`.
- Judgments `839165ab…` (owner, judge of record; AI first pass
  `09b82dcc…` by claude-opus-5-5, owner-reviewed; clarifications
  `cce59bb5…`).
- Code revision 02b5eba plus the uncommitted Phase 6 worktree
  (recorded dirty in `freeze.json`); full-text is the unchanged
  default; nothing committed.

Reproduce: `uv run python scripts/retrieval_eval/run_phase6.py --cases
data/phase6/blind/blind_cases_phase6_format.json --split
blind_confirmation --live --price-verified --ceiling-usd 0.05 --out
data/phase6/blind_raw.json` (paid calls; needs `OPENAI_API_KEY`),
then `uv run python scripts/retrieval_eval/derive_blind_configs.py`
(re-derives `(c)` at the frozen cutoff),
`uv run python scripts/retrieval_eval/score_blind.py`, `make check`.
Cutoff selection from the dev run:
`uv run python scripts/retrieval_eval/analyze_phase6.py --raw
data/phase6/dev_live_raw.json`.

## Limits

Adversarial 20-case set, not production traffic; small samples;
pool-based recall; development labels partly AI-assisted; the cutoff
was tuned on non-food probes and is not expected to catch
food-sounding no-match queries (not tested); embedding outage fails
closed (recorded error, no silent fallback).
`tests/test_embeddings_pg.py` has 5 failures on a disposable pgvector
DB — `test_stale_renderer_rows_excluded_from_search`,
`test_vector_pantry_boost_orders_before_distance_ties`,
`test_embedding_dimension_check_accepts_registry_dims`,
`test_cli_resume_embeds_zero_chunks_on_second_run`,
`test_cli_crash_recovery_reembeds_idempotently` — reproduced on a clean
02b5eba checkout, so pre-existing and not Phase 6, although Phase 5
recorded the tier as passing. Deferred; not fixed in Phase 6.
