# Milestone 3: decision recommendations and supporting sources

Prepared 2026-09-29 with AI assistance. Companion to
[Owner decisions pending](owner-decisions-pending.md), using the product goals in
the [AI Engineering Transition Plan](../../ai-learning-plan.md).

**Status: recommendations for owner review, not recorded owner decisions.**
This document does not approve spending, database writes, backup deletion, or
milestone acceptance. It does not replace the pending-decision record.

The recommendation is to fix recipe grounding and the Epicure policy mismatch,
correct the evaluation labels, and run the small live evaluation before Phase 5.
Use that run to find real-model failures before adding web search.

External sources explain engineering principles. Project files establish the
intended product behavior and observed implementation. The choices below are
project-specific judgments; the external sources do not prescribe this exact
architecture, budget, or case count.

## Decision overview

| Decision | Recommendation | Qualification |
| --- | --- | --- |
| 1. Checkpoint A | Qualified acceptance of the demonstrated loop mechanics | Keep successful-stop and grounding findings open until fixed; scripted runs do not establish model quality. |
| 2a. Recipe evidence | Require same-session retrieval and claim support | Include earlier turns of a resumed session; preserve dataset-qualified identity. |
| 2b. Epicure skips | Fix the direct-recipe policy mismatch; add a narrow pairing guard and evaluate skip quality | Single-recipe output must not require skipping Epicure. |
| 3a. Live budget | Propose a total cap of $0.15 | Enforce it before calls, including retries and query embeddings. |
| 3b. Database | A only with verified trial isolation; otherwise B | Fresh session state and stable corpus versions matter more than the database name. |
| 3c. Timing | After the fixes, before Phase 5 | Eight scenarios are an initial diagnostic, not a reliability benchmark. |
| 4. Technique labels | Change tq-05 and tq-11; keep the other seven proposed labels | Grade relevance against stored content; report coverage gaps separately. |
| 5. Housekeeping | Keep both backups and leave fixture databases alone | Verify backup contents and recovery coverage before any later deletion. |

## 1. Checkpoint A: review the Phase 3 trajectories

**Recommendation:** accept only what the scripted packet demonstrates, with
explicit reservations. These are suggested review answers, not a substitute for
the owner's answers in `evals/phase3_agent/owner_review.json`.

| Question | Suggested review answer | Follow-up |
| --- | --- | --- |
| Q1. Are the questions sensible? | Mostly. Asking about yogurt is useful when availability affects a candidate recipe. The empty-retrieval question is too generic. | Prefer a concrete choice, such as accepting a similar dessert, without pretending a suitable alternative has already been found. |
| Q2. Does it stop appropriately? | The demonstrated limit stops are reasonable. Some sufficient-evidence stops are premature because grounding and skip checks are incomplete. | Fix decisions 2a and 2b. Keep recovery messages clear about retrying the current session versus starting another. |
| Q3. Does it preserve answers? | Yes, for the demonstrated yogurt answer and vegetarian-constraint cases. | Verify the same behavior with the real provider and resumed sessions. Constraint persistence alone does not prove every proposed recipe satisfies it. |
| Q4. Does it present adaptations as source facts? | No in the displayed adaptation wording, but the broader source-grounding check is incomplete. | Keep the unverified-substitution label and fix unretrieved recipe citations. A label does not validate substitution feasibility. |

Also flag relevance in the live review: the packet offers lentil soup for a
chicken request without explaining why it is an alternative. Successful loop
termination does not establish that the user's request was fulfilled.

**Sources and their role:**

- [Phase 3 review packet](../evals/phase3_agent/REVIEW.md): the actual scripted
  outputs supporting these judgments.
- [Learning plan](../../ai-learning-plan.md): clarification should materially
  affect the recommendation, confirmed answers must persist, and adaptations
  must remain explicit.
- [OpenAI: Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices):
  supports task-specific evaluation and combining metrics with human judgment.
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  supports inspecting transcripts to distinguish agent mistakes from grading
  mistakes. Applying that principle here means reviewing real trajectories as
  well as the scripted packet.

## 2a. Require retrieved evidence for recipe recommendations

**Recommendation: yes, before the live run.**

Validate that each recommended `(dataset_id, source_id)` was returned by a
successful `search_recipes` or `get_recipe` call in this session. Evidence from
earlier turns must remain usable when the session resumes. A matching ID in the
database is not sufficient.

Also check evidence sufficiency: a search result containing only an ID and title
does not support quantities or cooking instructions. Retrieve the complete recipe
before constructing a final recipe or plan when the available result lacks those
details. Keep existing source-value validation alongside the retrieval check.

Acceptance examples for the fix:

- A valid database ID that was never retrieved is rejected.
- An identical source ID from a different dataset does not satisfy the check.
- A resumed session can use its previously retrieved evidence.
- Failed lookups and unrelated session evidence do not establish support.

**Sources and their role:**

- [Learning plan](../../ai-learning-plan.md), Milestones 2 and 3: requires
  complete source recipes for final answers and recommendations referencing
  retrieved recipes.
- [Recipe validators](../src/culinary_copilot/agent/validate.py) and
  [agent loop](../src/culinary_copilot/agent/loop.py): current checks establish
  database existence and quantity matching, but the option path lacks the
  required same-session retrieval check.
- [Ragas: Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/):
  defines faithfulness through support for response claims in retrieved context.
  The same-session identity check is our implementation recommendation for
  enforcing provenance, not a Ragas requirement.

## 2b. Align Epicure skip behavior with the product policy

**Recommendation: evaluation plus narrow deterministic checks, with a policy
correction beyond the original options.**

The learning plan explicitly says to query and evaluate Epicure even for specific
dish requests. Therefore, remove `direct_recipe_lookup` as an exemption unless
the owner intentionally changes that product policy.

The loop currently couples a single submitted recipe to that exemption. Separate
the two concerns: a direct dish request may receive one recipe while still
consulting Epicure. Preserve the documented case where filtering leaves only one
valid candidate, with the reason recorded.

For `simple_technique_question`, add a narrow guard against clearly mixed requests
such as “boil an egg, and what soup goes with it?” Keep this case and the
vegetarian-dinner misuse as regressions. A phrase guard is a limited safeguard;
it cannot establish semantic correctness across paraphrases or languages.

Treat Epicure unavailability as a recorded degraded mode, not evidence that a
task was simple. Check that the claimed unavailability matches configuration or
tool outcomes. Measure consultation, skip justification, and useful evaluation
of suggestions separately in Phase 7.

**Sources and their role:**

- [Learning plan: Epicure policy](../../ai-learning-plan.md): the controlling
  product requirement; research cannot choose this preference for the owner.
- [Agent loop](../src/culinary_copilot/agent/loop.py),
  `EPICURE_SKIP_ALLOWLIST` and the single-option validation branch: evidence of
  the policy mismatch and coupling.
- [Review packet](../evals/phase3_agent/REVIEW.md): concrete unjustified skips.
- [OpenAI: Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices):
  distinguishes tool selection and argument correctness from final-answer
  correctness. This supports evaluating Epicure behavior explicitly.

An eventual comparison of mandatory versus discretionary Epicure consultation
could inform a future policy change. It is not needed to resolve the current
implementation mismatch.

## 3a. Propose $0.15 for the live evaluation

**Recommendation: retain the proposed cap, subject to verified enforcement.**

The official Standard short-context prices checked during this review match the
plan: `gpt-6-luna` input $0.10 and output $0.50 per million tokens.
[Source: OpenAI pricing](https://developers.openai.com/api/docs/pricing).

Under those rates, with actual per-attempt limits of 30,000 input and 12,000
output tokens:

```text
Per attempt: 30,000 / 1,000,000 × $0.10
           + 12,000 / 1,000,000 × $0.50 = $0.009
Eight scenarios × two attempts × $0.009 = $0.144
```

This is conditional arithmetic for model tokens. It is not yet proof of a hard
end-to-end cost bound. The current pre-call input estimate uses characters
divided by four, and query embeddings also cost money. The $0.006 difference
must not be assumed sufficient without accounting for those calls.

Recommended runner behavior:

1. Verify the model, applicable pricing tier, rates, and remaining milestone
   budget before starting.
2. Count the complete input payload before each model call. OpenAI documents an
   input-token counting endpoint that includes tools and request structure.
3. Reserve the input cost plus maximum permitted output cost before dispatch;
   include embeddings and all retry paths in the same spending ledger.
4. Reconcile with reported usage. Treat uncertain charges conservatively rather
   than freeing a reservation after an ambiguous provider failure.
5. Stop before another call cannot fit. Finishing fewer scenarios is preferable
   to exceeding the approved cap.

Do not present the scripted output sizes as a reliable live-cost forecast.

**Additional sources and their role:**

- [OpenAI: Counting tokens](https://developers.openai.com/api/docs/guides/token-counting):
  supports replacing character estimates with counts for the complete request;
  documents that output limits include non-visible generated tokens.
- [Live plan](../evals/phase3_agent/LIVE_PLAN.md) and
  [agent loop](../src/culinary_copilot/agent/loop.py): sources for scenario counts,
  token ceilings, current estimation, and the existing stop design.
- [Milestone execution plan](milestone-3-execution-plan.md), Checkpoint 0:
  records the milestone budget and live-run authorization process.

The pre-call reservation approach is an engineering recommendation derived from
the cap requirement. Neither the documentation nor the arithmetic authorizes a
paid run.

## 3b. Choose the database based on trial isolation

**Recommendation: A only if isolation is verified; otherwise B.**

For this local diagnostic, sharing a stable recipe/technique corpus can be
reasonable. Each independent trial must start with fresh session state, use only
its own events and evidence, and avoid corpus writes. Deliberate ask-and-resume
turns belong to the same trial. A fresh rerun must not inherit the first
attempt's answers or retrieved evidence.

Record the target database and corpus versions, identify evaluation sessions,
and verify write scope before choosing A. Session labels help tracking but do
not enforce isolation. If these properties cannot be established, use an
explicitly identified disposable database.

For B, verify the restore's schema, corpus, and required technique embeddings.
The pending document describes the newer backup both as including migrations
005–007 and as being taken before 006/007; that discrepancy is unresolved.
Do not assume restoring it reproduces the current application state.

**Sources and their role:**

- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  recommends clean, isolated trials because shared state can distort results.
  It does not mandate a separate database for every evaluation.
- [Pending decisions](owner-decisions-pending.md) and
  [live plan](../evals/phase3_agent/LIVE_PLAN.md): the actual database choices and
  conflicting migration/backup descriptions.
- [Repository guidance](../AGENTS.md): database write tests use disposable
  databases; application writes require task authorization. This recommendation
  does not change the default test rule or authorize application writes.

## 3c. Run before Phase 5, after the fixes

**Recommendation: evaluate now in the development sequence, rather than waiting
for Phase 7.**

Build and test the runner after its plan is approved, then perform the authorized
live diagnostic after grounding, Epicure policy, budget accounting, and database
checks pass. Include the Phase 4 technique tool in the updated live plan.

Freeze the expected behavior before running. Grade task completion, constraint
adherence, evidence support, clarification, Epicure behavior, and termination.
Keep every attempt and report first-attempt outcomes separately from outcomes
after retry; a recovered success must not erase the initial failure.

Eight scenarios can expose integration failures. They do not establish a stable
success rate. Retain the broader Phase 7 evaluation and use these failures to
improve its cases. A real model may never trigger a scripted no-progress path;
keep deterministic failure injection for guaranteed coverage of that mechanism.

**Sources and their role:**

- [OpenAI: Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices):
  supports early, scoped evaluation and explicit success criteria.
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  supports starting with a small useful suite and examining individual trials.
  The eight-scenario scope is our diagnostic choice, not its prescribed sample
  size.
- [Live plan](../evals/phase3_agent/LIVE_PLAN.md) and
  [milestone execution plan](milestone-3-execution-plan.md): existing initial
  scenarios and the planned broader Phase 7 evaluation.

## 4. Correct the technique relevance labels

**Recommendation: change tq-05 and tq-11; keep the other seven proposed labels.**

These judgments concern the locally stored document versions. External pages can
change, and linked PDFs are not available evidence unless ingested. The local
files below are git-ignored corpus artifacts; their adjacent `.meta.json` files
record source URLs, retrieval timestamps, revisions, and checksums. These links
require the local corpus and may not resolve in a fresh checkout.

| Case | Recommended decision | Evidence and rationale |
| --- | --- | --- |
| tq-01: sear chicken crust | Keep Searing. | [Stored Searing](../data/technique-corpus/tech-sear-01.txt) describes forming a browned surface crust, including on poultry. |
| tq-02: braise moist heat | Keep Braising. | [Stored Braising](../data/technique-corpus/tech-braise-04.txt) describes browning followed by covered cooking in liquid. |
| tq-04: chicken internal temperature | Keep FDA Safe Food Handling. | [Stored FDA source](../data/technique-corpus/tech-fda-safe-32.txt) contains the relevant poultry-temperature table. |
| tq-05: chill freeze storage | Replace `tech-fda-kitchen-33` with `tech-fda-safe-32`; keep `tech-freeze-R6`. | [Stored Kitchen page](../data/technique-corpus/tech-fda-kitchen-33.txt) mainly points to resources. [Safe Food Handling](../data/technique-corpus/tech-fda-safe-32.txt) contains chilling/storage guidance, while [Freezing](../data/technique-corpus/tech-freeze-R6.txt) covers preservation and packaging. |
| tq-07: sauté high heat | Keep Sautéing. | [Stored Sautéing](../data/technique-corpus/tech-saute-02.txt) directly covers the requested method. Another page ranking first does not justify changing the label. |
| tq-11: fix split curdled yogurt sauce | Remove Sauce as a relevant document; record a known coverage gap. | [Stored Sauce](../data/technique-corpus/tech-sauce-15.txt) does not provide the requested repair guidance. A topical word match is insufficient. Adding suitable evidence later is a separate corpus change. |
| tq-12: fluffy rice | Keep Boiling Rice and Cooked rice. | [Boiling Rice](../data/technique-corpus/tech-rice-boil-21.txt) directly addresses fluffy texture. [Cooked rice](../data/technique-corpus/tech-rice-23.txt) supplies supporting variety/draining context. This uses a supporting-evidence relevance rule, not a requirement that every page independently give a complete answer. |
| tq-15: pink chicken inside | Keep FDA Safe Food Handling. | [Stored FDA source](../data/technique-corpus/tech-fda-safe-32.txt) addresses the unreliability of color as a safety indicator. Keep the retrieval miss visible. |
| tq-16: browned pan sauce | Keep Deglazing and Sautéing. | [Deglazing](../data/technique-corpus/tech-deglaze-31.txt) directly covers pan sauce; [Sautéing](../data/technique-corpus/tech-saute-02.txt) explicitly connects pan residue with sauce preparation. |

The FDA source is also available from its
[official publisher](https://www.fda.gov/food/buy-store-serve-safe-food/safe-food-handling).
The labels above were assessed against the stored snapshot, not a claim that a
newer online version has been imported.

After owner review:

1. Version the corrected labels and recompute the freeze hash, recording the
   reason and actual reviewer. Do not call AI recommendations human acceptance.
2. Preserve the original result files and their old label hash.
3. Recompute HitRate@5 and MRR from the saved rankings for both retrieval modes.
   Label this a rescore, not a new retrieval run. No embedding call is needed
   when only labels change and the saved rankings are sufficient.
4. Report retrieval scores over the 15 answerable cases and report the one
   coverage-gap case separately, with the denominator change disclosed.
5. Evaluate whether the agent acknowledges missing evidence on tq-11. A
   retriever returning a loosely related page does not by itself demonstrate
   that the agent handled the gap correctly.

The current scorer includes all cases in its HitRate denominator. Merely setting
`relevant_docs` to an empty list would count tq-11 as a miss; implement the
explicit coverage-gap treatment before publishing corrected aggregates.

**Methodology and implementation sources:**

- [Frozen cases](../evals/technique_retrieval/cases.json): original labels,
  freeze rule, and existing disclosure of exploratory query changes.
- [Full-text results](../evals/technique_retrieval/baseline_fulltext.json) and
  [vector results](../evals/technique_retrieval/vector_run.json): saved rankings
  available for offline rescoring.
- [Evaluation runner](../scripts/techniques/eval_baseline.py): current scoring
  and denominator behavior.
- [Ragas: Context recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/):
  explains evaluating retrieval against relevant reference evidence. Separating
  unanswerable cases is our reporting recommendation; this project uses
  HitRate@5 and MRR, not a claim of identical Ragas scoring.

## 5. Keep recovery artifacts for now

**Recommendation: keep both backups and leave the fixture databases alone.**

The small storage saving does not justify deleting a recovery point while its
replacement's coverage is disputed. Before later deletion, verify what each dump
contains and demonstrate that the retained artifact restores the required state
in a disposable database. Keep the necessary recovery artifacts through
milestone acceptance.

This is a practical project judgment, not a retention period established by
internet research. No backup inspection, restore, or deletion was performed as
part of this recommendation document.

**Sources and their role:**

- [Repository guidance](../AGENTS.md): explicitly requires preserving recovery
  artifacts and verifying a recoverable backup before an authorized migration.
- [Pending decisions](owner-decisions-pending.md), sections 3b and 5: identifies
  the two backups and contains the unresolved newer-backup descriptions.

## Deferred decisions and next sequence

Keep Phase 5 search-provider/budget approval, the technique retrieval default,
and final milestone acceptance in their existing checkpoints. The current
label corrections and eight-scenario diagnostic do not settle those choices.

This follows the [milestone execution plan](milestone-3-execution-plan.md) and
[learning plan](../../ai-learning-plan.md): compare retrieval modes in Phase 7,
enforce user permission for web search, and review real trajectories with sources
before final acceptance.

Recommended sequence after the owner resolves the pending choices:

1. Record the actual owner answers, preserving this document as advice.
2. Fix recipe grounding and Epicure policy; regenerate the scripted review.
3. Correct accepted relevance labels and rescore saved retrieval results.
4. Update the live plan, build the runner, and validate it offline.
5. Perform the separately authorized live diagnostic and review every attempt.
6. Use the findings to proceed into Phase 5 and expand Phase 7's evaluation.
