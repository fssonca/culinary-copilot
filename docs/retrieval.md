# Phase 1 retrieval guide: ready requests to bounded recipe evidence

Backend-only (no frontend, no embeddings, no generation). Consumes the
current clarification group plus expected revisions and returns bounded
evidence summaries of full source documents via the existing repository.
Generation must later fetch complete documents using the exact
`(dataset_id, source_id)` identities.

## Entry point

```sh
# 1. Create / clarify a group until ready_for_retrieval is true.
curl -X POST http://localhost:8000/api/v1/clarification/groups \
  -H 'Content-Type: application/json' \
  -d '{"message": "chicken dinner", "dish": "chicken curry",
       "request": {"ingredients": ["chicken"], "time_minutes": 30},
       "use_llm": false}'

# 2. Retrieve bounded evidence summaries for that group (revision-pinned).
curl -X POST http://localhost:8000/api/v1/retrieval/search \
  -H 'Content-Type: application/json' \
  -d '{"group_id": "<grp>", "request_revision": 1, "group_revision": 1,
       "limit": 5}'
```

Request body: `group_id`, `request_revision`, `group_revision` (both required),
optional `limit` (1–10, default 5) and optional `dataset_id` (one of the
supported datasets; omitted = combined corpus).

Error behavior:

| Situation | Status | Note |
| --- | --- | --- |
| Unknown group | 404 | `clarification group not found` |
| Stale `request_revision`/`group_revision` (before or after I/O) | 409 | refetch the group and retry |
| Superseded group (a newer group exists for the request) | 409 | names the current group; refetch and retry |
| Unsupported `dataset_id` / bad `limit` | 422 | clear message, no state change |
| Recipe corpus unavailable | 503 | no detail leak |

Older groups stay readable as history through the clarification group
endpoints; only retrieval requires the current group.

## Deterministic request-to-query mapping

Implemented in `retrieval/query.py` (see module docstring for the exact rules).
Eligibility and ranking are separate (owner decision D1):

- Dish + pantry: the dish text alone decides eligibility. Available pantry
  ingredients only influence ranking through a separate OR-scored boost; a
  dish match with zero pantry overlap remains eligible. A result never
  implies the recipe needs no additional ingredients.
- Pantry-only: eligibility is overlap with at least one available
  ingredient (exact canonical names, so multiword meaning such as
  "olive oil" is preserved), ranked by overlap count.
- There is currently no must-have ingredient field, so
  `required_ingredients` is always empty here. Explicit
  required-ingredient filters stay mandatory containment wherever the
  repository supports them; nothing here relaxes them.
- Conservativeness: the mapping consumes structured state only, never raw
  free-text messages. No stopword list is applied — dish terms, negation,
  and restrictions stay verbatim in the query text. The original input and
  every transformation are recorded in the response. No case-specific rules.
- `time_minutes` maps to the repository `max_minutes` ceiling (shared
  policy in `recipes/durations.py`, applied before ranking/`LIMIT` in both
  standalone search and retrieval): only a finite positive reported total
  can satisfy a ceiling. Missing, unverified zero, negative, and
  non-finite totals are unknown and excluded. The ceiling is never
  silently relaxed: an over-strict ceiling yields an empty result with an
  explanation, not a widened search.
- Cuisine, preferences, dietary constraints, equipment, substitution choices,
  portions and task scope are preserved in clarification state but not
  enforced by full-text search. They are returned unchanged as
  `unsupported_constraints` / `unenforced_constraints`, and the top-level
  `constraints_not_verified` list (dietary compatibility, allergy safety,
  nutrition, quantities/units, scaling, completeness, equipment) is always
  present. Readiness for retrieval is not safety, nutrition, completeness, or
  scaling approval.

## Response shape

- `outcome: "ready"` with `results` (bounded summaries), or
  `"not_ready"` with `not_ready_reason` (`conflicting` / `blocked` /
  `needs_clarification`), `blockers`, and the would-be query. Blocked
  essentials match stable blocker codes, never message substrings.
  Not-ready runs no recipe search. Caller-sent ready flags are never
  trusted: readiness is recomputed from authoritative store state on every
  call.
- Every response carries the snapshot `request_revision`/`group_revision` it
  was computed from, the `readiness_note` disclaimer,
  `unenforced_constraints`, `constraints_not_verified`, an `evidence_note`
  (summaries, not complete recipes) with `evidence_limits`, and the query
  mapping with its explanation.
- Every ready response distinguishes `datasets_searched` from
  `datasets_in_results`. Under a ceiling the explanation states that
  recipes without a usable reported total duration are excluded.
  Dataset-specific claims always come from measured snapshots, never from
  hardcoded dataset properties.
- Each evidence record carries the exact `(dataset_id, source_id)` identity,
  `evidence_type: "bounded_summary"`, title, provenance, flags,
  capabilities and quality metadata, explicit `unknowns`, a bounded source
  excerpt, and raw-vs-usable durations (`total_minutes_reported` preserved;
  `duration_status` one of `reported_positive` / `reported_zero_unverified`
  / `missing`). Older Food.com documents keep `capabilities_unknown`
  rather than implying validation. `nutrition_values_present` records
  value presence only; nutrition verification stays unknown, as do dietary
  compatibility, allergy safety, quantities, and scaling. Servings count as
  known only for finite non-boolean numerics. No per-recipe source URL
  exists in the corpus (never invented).
- Empty results explain the query, ceiling, and dataset scope and state that
  no constraints were dropped or relaxed.

Concurrency: the service snapshots the store, releases the lock, runs blocking
repository work in worker threads (`asyncio.to_thread`, never on the event
loop), then re-reads the store and fails closed with 409 when an intervening
edit advanced either revision or a newer group superseded the requested one.
No store lock is held across I/O.

## Evaluation (AI-assisted, owner-accepted policy; NOT independently verified)

Status: "AI-assisted review accepted by the project owner; not independently
human-verified or culinarily validated." No blanket approval of all labels.

- Cases: `evals/cases/phase1_retrieval.json` — v2, 52 cases: 32 development
  (`DEV-01..DEV-32`) plus 20 held-out (`HELD-01..HELD-20`);
  `retrieval_only` vs `integration` kinds are separated, behavioral
  expectations are separate from relevance, and `HELD-10`/`HELD-11` share
  one aggregate intent. Validate with
  `uv run python scripts/retrieval_eval/validate_cases.py`. Do not tune on
  held-out failures.
- Rubric: `evals/rubric_v1.md` — topical grades, the displayed-source-evidence
  standard for constraint judgments, system-enforcement separation,
  suitability aggregation, and no-match discipline.
- Recorded judgments: `evals/results/phase1/checkpoint1_corrections.json`
  (AI second pass plus the owner decision layer; individual recipe labels
  remain AI-labeled). The deterministic assembler
  (`scripts/retrieval_eval/assemble_packet.py`) joins recorded judgments
  into fresh read-only candidate pools without regenerating them; new or
  unmatched candidates stay `unlabelled`. Historical packets are preserved.
- Metrics: `scripts/retrieval_eval/metrics.py` keeps topical grade,
  constraint evidence, and suitability separate (`not_applicable` when
  there are no constraints) and reports topical Recall@5/MRR plus joint
  relevance-and-suitability. Unjudged candidates are excluded, never
  zero-graded; cases without judgments are listed separately, never treated
  as verified no-match.
- Calibration packet: `evals/results/phase1/calibration_packet_v2.md`
  (+ `.json`) — 14 prior slots plus 7 new AI-proposed anchors, built by
  `scripts/retrieval_eval/build_calibration_packet.py` from structured
  records. Each item shows exact query/dish/pantry/constraints/scope/time,
  input provenance (original messages were never recorded and are never
  invented), production-equivalent output kept distinct from the broader
  annotation pool, and four separate layers (AI proposal, accepted combined
  review, owner acceptance, new AI-authored adjudication). The working
  `calibration_sample.*` and pre-acceptance snapshots are preserved
  untouched.
- Frozen inputs: `evals/results/phase1/phase2_inputs_freeze.json`, built by
  `scripts/retrieval_eval/freeze_inputs.py` (case/rubric hashes,
  corpus/search fingerprints, commands, discovery methods, exposure, label
  provenance, coverage). Rerun after the authorized rebuild for the
  post-rebuild fingerprint.
- Empty candidate pools reflect one query/filter/dataset slice only and do not
  prove no relevant recipe exists.

## Food.com search-text rebuild (pending owner approval)

Food.com keyword lists are stored character-split and never match as words.
The execution package at
`evals/results/phase1/foodcom_rebuild_package.md` holds the verified
dry-run (1216 checked / 1211 would update), the disposable-DB rehearsal
(`scripts/retrieval_eval/rehearse_rebuild.py`), backup/restore, exact
commands, and invariance checks. Do not run it before explicit go-ahead.

## Limits

`limit` 1–10 (default 5); query text truncated to 500 chars; excerpts to 600
chars; ingredient names to 30 per record. Full-text search remains the default;
Phase 5 vector/hybrid code is prepared offline (`recipes/vector_search.py`,
`embeddings/`, migration `004` skipped on stock Postgres) and inactive until
the approved pgvector activation and embedding job (see
[execution package](phase5-execution-package.md)). No vectors are queried in
production paths; fake-vector tests prove plumbing only, never relevance.

Ready retrieval responses carry two additive keys: `retrieval_mode`
(`fulltext` | `vector` | `hybrid`; default `fulltext`, which makes zero
embedding calls) and `retrieval_fallback` (null, or a disclosure string such
as `fulltext (embeddings unavailable, disclosed)` when an explicitly
allowed fallback was used — never silent).
