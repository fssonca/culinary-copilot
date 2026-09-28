# ADR 0001: Retrieval default after the Phase 6 blind comparison

Status: **result recorded, default unchanged; wiring steps 1–2 and 5
done in Milestone 3, Phase 2 (see docs/tools.md)**. Full-text remains
the default. Flipping to `vector_c` requires a separate owner
approval; the exact change is proposed below, not applied.

## Context

Phase 6 compared three frozen retrieval modes blind on 20 adversarial
requests (owner-judged, `docs/scoreboard.md`): full-text (Phase 2
baseline, unchanged), vector and hybrid over `text-embedding-3-small`
(1536 dims, renderer v1) with `rrf_k = 60`, `vector_candidates_n = 20`
(fixed, not tuned) and one tunable cosine-distance cutoff (0.66,
selected on development data: 19/20 non-food abstention, hybrid
HitRate@5 0.974).

## Decision (recorded, not enacted)

The frozen §7 rule selects **vector_c** (vector mode, cutoff 0.66):
net HitRate wins +6 (≥ 2), MRR 0.703 and nDCG 0.856 (both ≥ full-text),
0 violations, abstention total 0 ≤ full-text's 0. Hybrid_c passes every
gate too; the tie-break (HitRate tied 0.812) falls to MRR, 0.703 vs
0.656 — a gap resting on **2 of 16 requests** (first-qualifying-rank
2-vs-4 on BLIND-01, 1-vs-2 on BLIND-10). The outcome is unchanged under
the AI first-pass grades and under a vegetarian reading of BLIND-18.

## Tradeoffs

- **Recall vs latency/cost:** vector_c lifts blind HitRate 0.438 →
  0.812, but every search pays one embedding call (measured p50
  ~565–645 ms, p95 ~715–895 ms) plus ≈6 tokens ≈ $0.0000001
  (measured: 1,892 actual tokens over 308 paid calls), versus ~7 ms
  end-to-end for full-text with zero paid calls.
- **Availability:** embedding outage fails closed — a recorded error,
  never a silent full-text fallback (`allow_fallback = False`). A
  vector default converts an embeddings outage into search outages.
- **Abstention:** the cutoff gives clean 1.000 abstention on the four
  abstention cases (uncut vector returned 15 results there); without it
  vector is the leakiest mode on out-of-distribution input.
- **Thin margin:** the vector-vs-hybrid choice rests on two requests;
  hybrid keeps full-text's exact-match behavior blended in and never
  abstains while full-text is non-empty.
- **Untested:** food-sounding no-match queries (a cutoff tuned on
  non-food probes is not expected to catch them); HNSW indexing (vector
  search is currently an exact scan at ~65 ms p50).

## Proposed default-flip change (proposal only)

**An environment variable alone cannot enact this.** The `RETRIEVAL_*`
and `EMBEDDINGS_ENABLED` settings are validated at load, but no request
path reads them:
- `POST /api/v1/retrieval/search` calls `retrieve_for_group` without a
  mode, so it always gets full-text.
- The recommendation service runs its own full-text search.

 Adopting `vector_c` therefore needs a small, separately approved code
 change (steps 1–2 and 5 done in Phase 2; 3–4 open):

 1. **Wire the retrieval endpoint.** ✅ Done (Phase 2): `api/retrieval.py`
    passes `settings.retrieval_mode`, `retrieval_vector_cutoff`,
    `retrieval_fulltext_gate`, `retrieval_rrf_k`,
    `retrieval_vector_candidates` and the embedding model/dimension
    into `retrieve_for_group`. Code default stays `fulltext`.
 2. **Construct the query embedding provider at startup** ✅ Done
    (Phase 2): `api/app.py` builds it only when `EMBEDDINGS_ENABLED`
    is set, with a model/dimension check (`check_model_dimension`;
    `Settings` already refuses registry mismatches at load).
 3. **Decide outage behaviour.** Open — still fail closed
    (`allow_fallback = False`); an embeddings outage makes search
    unavailable. No disclosed-fallback setting added.
 4. **Decide the recommendations path,** as a separate choice: open —
    recommendations keep explicit full-text (no `retrieval_mode`
    reference; asserted by `test_recommendations_path_still_fulltext`).
 5. **Add tests** ✅ Done (Phase 2): `tests/test_tools.py` proves the
    settings reach the request path and the full-text default makes
    zero embedding calls; `tests/test_tools_pg.py` covers the
    permission gate on a disposable DB.

Once wired, enact it by environment, keeping `fulltext` as the code
default:

```sh
EMBEDDINGS_ENABLED=true
RETRIEVAL_MODE=vector
RETRIEVAL_VECTOR_CUTOFF=0.66
```

Runtime cost: one paid embedding per search (≈6 tokens, p50 ~565–645 ms).
Rollback: set `RETRIEVAL_MODE=fulltext` (or unset it) and restart. It
needs no migration or data change, and the full-text path stays
untouched.

## Consequences

This ADR makes no code or default changes. If approved:
1. implement and test the wiring above;
2. then switch by environment;
3. monitor embedding latency and error rate, and the abstention rate;
4. roll back on any regression.
