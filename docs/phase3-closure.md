# Phase 3 closure — accepted backend milestone

**Accepted by Fernando Sousa on 2026-09-24**, based on the AI-assisted close-out
report and the recommendation to accept the bounded development milestone.
This is not independent human source verification or culinary validation.
The owner checkpoint is closed. See the authoritative
[decision record](../evals/phase3_review/owner_decisions.json).

## Delivered scope

| Requirement | Evidence / boundary |
|---|---|
| Revision-pinned recommendation endpoint and outcomes | Recommendation, clarification and insufficient-evidence paths implemented |
| Fixed workflow and Epicure consultation | Distinct consultation, skip and degraded outcomes; no recipe adaptations |
| Bounded provider and structured selection | Typed propositions, server wording, bounded attempts and serialized inputs |
| Grounded source rendering | Exact identities, complete reference coverage, explicit unknowns; structural admission is not semantic completeness |
| Source review and enrichment proposals | Six examples and three separate proposals in the [review spec](../evals/phase3_review/README.md) |
| Native tool exercise | Repaired two-turn continuation exercised in FOLLOWUP-02 |
| Failure and quarantine checks | Offline regressions and disposable-Postgres tests; quarantined-only rows excluded |

Implementation and live evidence have been accepted with the limitations below.
No data corrections were applied as part of acceptance.

## Final live evidence

The repaired contract was exercised in `evals/results/phase3_followup_live/`.
Both cases returned HTTP 200 recommendations. Reported usage yielded a combined
cost of **$0.0002995**, below the $0.05 authorized ceiling. No unknown usage was
charged at reservation. This is a two-case behavioral check, not a reliability
or culinary-quality benchmark.

| Case | Result | Usefulness |
|---|---|---|
| FOLLOWUP-01: ordinary chicken curry within 40 minutes | Exact source rendering; valid title/time propositions; invalid optional ingredient reason omitted and recorded | Relevant but weak: 000322 lacks quantities and omits ingredients used in its steps; a better candidate was offered |
| FOLLOWUP-02: native tool Thai chicken curry | Correct final instructions, matching source snapshot, bounded continuation and exact source rendering | Useful in the AI-assisted review, with missing-unit caveats |

Earlier run5/live10b evidence used the old free-text contract. Its corrected
usefulness count was three useful results from **one distinct recipe**, one
conditionally useful result, one with usefulness not established, and one
synthetic containment example. In particular, an overnight French-toast
casserole did not establish usefulness for a simple toast request. Historical
outputs remain local and unchanged; they do not verify the repaired contract.

## Accepted review decisions

- **ENR-01:** accept the coconut-milk name correction as a proposal record only.
- **ENR-02:** accept that the missing nam pla unit must remain unknown.
- **ENR-03:** accept the ingredient-list inconsistency as a documented defect only.

The original AI specs retain their pre-acceptance status and attribution. The
separate owner decision records acceptance and hashes the accepted inputs.
Accepting a proposal does not apply it or upgrade corpus capabilities.

**Food.com 000322 is still presentable today.** It has no recorded error-severity
quality issue. An isolated regression fixture proves that recording the proposed
issue would exclude it from model evidence and leave an incomplete discovery
pointer, but the application row was not changed. Responses disclose
`ingredient_list_vs_steps: not_checked`; this is a disclosure, not a repair.

## Accepted limitations and next work

- Selection quality and useful-result diversity remain limited.
- Source consistency and hostile/garbled source content are not automatically screened.
- Unknown quantities/units stay unknown; dietary constraints conservatively abstain.
- Epicure provides pairing context but does not adapt the recipe.
- Rejected proposition refs and abstention candidate details are not fully recorded.
- A later Foodie quarantine does not delete an earlier accepted row. Quarantined-only
  identities are excluded; accepted versions can coexist with quarantine history.
- Clarification state remains process-local, and streaming/complete telemetry are future work.

Phase 4 (streaming and telemetry) may proceed under the
[execution plan](week-2-execution-plan.md#4-add-streaming-and-complete-telemetry).
No further Phase 3 live run is required for this documentation close-out.

## Verification and retained evidence

The close-out report recorded `make check`: **579 passed, 1 skipped**, with
Ruff, formatting and mypy clean, including disposable-Postgres tests. See the
commit completion report for checks rerun during cleanup.

- Tracked: review specs, owner decision, hash-only manifest, case definitions,
  regression tests, review builder and maintained [evaluation procedure](../evals/phase3_live_plan.md).
- Local only: live runs and source-filled review packets under `evals/results/phase3*/`.
- Archived locally: superseded documentation and 22 redundant dry-run/rehearsal
  directories in `data/phase3-archive/20260924/superseded-docs-and-rehearsals.tar.gz`.
  Every archived file was checksum-verified before directory removal; its inventory
  is `manifest.json` beside the archive. The empty scoreboard and empty run3
  placeholders were removed. Phase 6 will create a measured scoreboard.

Rebuilding the source packet requires the retained local artifacts and matching
corpus; a Git checkout alone cannot reproduce the historical evidence.
