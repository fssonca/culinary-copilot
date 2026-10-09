# Checkpoint D: review and recommendations

Reviewed 2026-10-08 against `c99a69c`, the two Checkpoint D documents,
H5's local procedure/preview, H6's saved rankings and scoring code, and
the stored recipe evidence in an explicitly read-only database connection.
These are AI-prepared recommendations, not recorded owner decisions.

**Recommended direction:** keep production ranking unchanged; accept the
judgments below with clearer explanations; authorize the scoped H5 cleanup
after the executable procedure is reviewed and rehearsed; then run H8 on
a frozen build with demo limits and an enforced $0.15 campaign budget.
Push the branch when authorized; review H8 before deciding to merge.

| Decision | Recommendation |
|---|---|
| 1. H6 judgments | Retain the 18 proposed grades under the existing conventions; add the five judgments below. Correct the suitability and explanation issues. |
| 2. Ranking | **A: keep current ranking.** Develop an explicit dish-intent boost later, with broader development cases and a fresh held-out evaluation. |
| 3. Malformed recipes | Approve the bounded removal/quarantine plan, subject to the procedure corrections below. Preserve the 38 older quarantine records; defer title recovery. |
| 4. H8 | Freeze the final reviewed commit and cleaned corpus; two fresh sessions, demo limits, web off, $0.15 shared and enforced across all paid calls. Execution still needs the owner's go-ahead. |
| 5. Push | Push the reviewed branch. Merge only after reviewing H8 outcomes and explicitly accepting remaining limitations. |

**1. Suggested judgments**

The original 18 topical grades are defensible under the recorded broad-query
precedents: **row 3 = 1; every other row from 1–18 = 2**. Keep their suitability
values, with the explanation corrections below. This is an assessment of the
stored evidence, not independent verification of the original recipes.

| Rows | Suggested grade | Reason / qualification |
|---|---:|---|
| 1 | 2 | A pasta casserole answers a generic pasta request; pantry items are hints, not an exclusive shopping list. |
| 2 | 2 | Garlic is a named feature of the dressing; reported time is 5 minutes. |
| 3 | 1 | Retain the ingredient-query precedent for garlic in an accompanying ragout. This depends on that convention, not the literal “same dish family” rule. |
| 4–5 | 2 | Garlic is a named feature, consistent with the existing garlic-query convention. |
| 6, 9 | 2 | The requested apple pie. |
| 7–8 | 2 | Both are chicken dinners. Equipment suitability remains **unresolved**: the directions explicitly require a slow cooker or oven/baking sheet, while the user only lists a wok and does not say it is exclusive. |
| 10 | 2 | Soup matches the generic request. Missing pantry items do not impose an unstated vegetarian or pantry-only constraint. |
| 11, 15 | 2 | Egg drop soup matches; reported 20 minutes satisfies the recorded 60/45-minute limits. This is reported-time support. |
| 12–14 | 2 | Named chocolate-cake variants. |
| 16–18 | 2 | Tofu and vegetables are substantive components of these dishes. The request does not say vegan or vegetarian; row 18 contains beef stock. |

For the five new rows:

| Row | Recipe | Topical grade | Overall suitability | Evidence / note |
|---|---|---:|---|---|
| 19 | Grandma's Canned Corned Beef and Cabbage Soup | 2 | `not_applicable` | Cabbage soup variant; no vegetarian constraint. |
| 20 | Pasta e Fagioli (Pasta and Beans) | 2 | **`not suitable`** | **Vegan: `violated`**, because chicken broth is in the ingredients and instructions; the description also suggests Parmesan. **Gluten-free: `unresolved`**, because ditalini/small pasta is not specified gluten-free. Do not silently grade a hypothetical adaptation. |
| 21 | Traditional Bulgarian Soup (Shopi Style Soup) | 2 | `not_applicable` | Soup matches. Bacon, pork fat, eggs and cheese do not violate an unstated diet. Ingredient parsing defects such as `gbacon` remain a separate data-quality issue. |
| 22 | Ashanti Chicken | 2 | `not_applicable` | Description and directions identify stuffed chicken. Its stored ingredient list contains seasonings but omits chicken and rice: topical relevance does not establish that a complete sourced plan can be produced. |
| 23 | Chocolatetown Special Cake | 2 | `not_applicable` | Instructions explicitly use cocoa in a cake. Cocoa is absent from the stored ingredient list; record that completeness issue separately. |

Before treating these as a reusable grading policy, clarify how broad
ingredient queries differ from named-dish queries. “Garlic occurs in the
title” should not become a universal definition of relevance. For this
checkpoint, preserve the existing convention rather than changing only
the new labels to favor a ranking. Future calibration should apply one
explicit intent-based rule to both old and new results. Relevance should
reflect the information need, which can be ambiguous in a one-word query.
[Source: Stanford IR text, information needs and evaluation](https://nlp.stanford.edu/IR-book/html/htmledition/information-retrieval-system-evaluation-1.html).

The recipe-specific evidence is the local `recipes.document` identified by
the dataset/source pairs in the review sheet. External recipes with similar
titles should not replace that evidence. Constraint decisions follow
[`evals/rubric_v1.md`](../evals/rubric_v1.md), including its distinction
between individual constraint violations and overall `not suitable`.

**2. Keep current ranking, and correct the evidence presentation**

The title boost is promising, but not established as a better general ranking.
I agree with option A for H8. Defer both a global boost and title-first.

Four issues matter:

1. **“Recall@5” is actually Hit/Success@5.**
   `scripts/retrieval_eval/h6_ranking_compare.py` returns 1 when any relevant
   recipe occurs in the top five. It does not divide retrieved relevant
   recipes by all known relevant recipes. Rename the reported metric, retain
   the historical output, and explain the correction. The displayed numbers
   can remain numerically unchanged. Standard recall measures the fraction
   of relevant documents retrieved.
   [Source: Stanford IR text, precision and recall](https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-unranked-retrieval-sets-1.html).
2. **The five extra labels do not complete the judgment pool.** Across the
   17 cases changed by either experiment, the union of all three top-five
   lists contains 111 query–recipe pairs. After the existing labels plus the
   proposed 18, **50 pairs remain unjudged**; the five new judgments leave
   **45**. Across all 52 cases, 110 remain before adding those five.
   Complete the pooled top-five judgments for the development cases before
   choosing a ranking; present held-out results separately. Pooling across
   competing systems is an established way to build balanced judgments.
   [Source: Stanford IR text, assessing relevance](https://nlp.stanford.edu/IR-book/html/htmledition/assessing-relevance-1.html).
3. **The 23-case denominator is selected using the current ranking.**
   It is a useful paired diagnostic, not a full-suite score. For example,
   DEV-03, DEV-28 and HELD-07 have no judged current top-five hit and remain
   excluded even after their proposed new labels. Use a predetermined case
   set and report judgment coverage; missing judgments are not verified
   irrelevance. Label the three “lost every judged hit” cases as a finding
   against the original labels, not against the expanded layer.
4. **The weight and test separation need work.** Adobo helped choose 2.0,
   so success on adobo is a regression check, not independent evidence.
   AI-assisted held-out labeling is not inherently leakage; using exposed
   held-out outcomes to choose weights is the problem. Choose weights on
   development cases and reserve fresh unseen cases for confirmation.
   [Source: Stanford IR text, development versus test collections](https://nlp.stanford.edu/IR-book/html/htmledition/information-retrieval-system-evaluation-1.html).

For a future boost, require an explicit search intent (`dish`, `ingredient`,
or unknown), with current behavior for unknown intent. Evaluate more named
dishes and ingredient controls, and report suitable-result metrics alongside
topical ones. PostgreSQL supports weighting title and body evidence; that
capability does not establish that weight 2.0 is appropriate here.
[Source: PostgreSQL 17 ranking documentation](https://www.postgresql.org/docs/17/textsearch-controls.html#TEXTSEARCH-RANKING).

As a diagnostic only, rescoring the saved lists with all five new rows at
grade 2 gives title-boost Hit@5 = 0.913 and MRR@5 = 0.562 on the same 23
cases, versus current 0.783 / 0.523. These remain incomplete-pool results.
No judgment files or saved rankings were changed. Broader ranking work need
not delay H8 when production ranking stays unchanged.

**3. Approve the scoped cleanup after making its procedure executable**

Read-only checks confirm the starting counts: **16,033 recipes, 158 malformed
`summary` recipes, 443 quarantine rows, 14,817 foodie embedding rows**.
The proposed target counts are consistent with deleting those 158 and adding
201 quarantine records. This is removal/quarantine, not recovery of 158
usable recipes.

Accept the 38 additional quarantine entries as import history. Retain both
import IDs and reasons; report unique affected source rows separately from
quarantine-event counts. Leave manual title recovery for later. Some
descriptions name a dish, so describe the defect as missing a usable title
field, rather than asserting that every raw row contains no title evidence.

The local `data/h5-repair-preview/REINGESTION-PROCEDURE.md` needs these fixes
before execution:

- **Explicitly bind every rehearsal write to the disposable database.**
  Its displayed `load` command has no database override.
  `cmd_load` uses `Settings().database_url`, which can come from `.env`.
  The executable procedure must check the actual database/server identity
  and refuse a rehearsal against the application target.
- Use a frozen identity manifest, not only a dynamic title predicate.
  Implement count/fingerprint assertions as errors that abort the
  transaction; comments saying “else rollback” do not enforce anything.
- Make both states explicit: first application expects 158 condemned rows;
  rerun expects zero and verifies the already-completed target state.
  Reuse the same import ID on rerun so it cannot append another 201 records.
- Quarantine loading currently commits separately from deletion. Either
  make the operation atomic or document/test recovery from the intermediate
  “quarantine recorded, recipes still present” state. Verify aliases and
  unchanged survivors as already proposed.

Keep the verified restore and two-pass disposable rehearsal. PostgreSQL's
restore tooling supports error-stop and single-transaction restores; the
restored contents still need the proposed application checks.
[Source: PostgreSQL 17 pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).

The cache-version change is real, but “future full preparation would pay to
re-extract” is too absolute: preparation/finalization are offline, and
payment requires a later submission. Existing cache keys no longer match;
future ingestion should assess compatible saved responses before deciding
whether any paid extraction is needed. This targeted cleanup needs none.
Local sources: `recipes/llm_cache.py`, `recipes/llm_batch.py`, and
[`docs/hybrid-ingestion.md`](hybrid-ingestion.md).

**4. Approve H8's design with an actual spending guard**

Use **demo limits** to test the two previously failing workflows under the
same allowances. Keep ordinary defaults unchanged. Passing these two
sessions supports a narrow demonstration under demo limits; it does not
establish reliability or success under ordinary limits. Separate attempts
and transcript review matter because agent outcomes vary.
[Source: Anthropic, agent evaluation guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).

Freeze after the cleanup procedure and any spending-guard work are reviewed:
record the actual final commit, corpus fingerprint/import ID/counts, model,
reasoning effort, service tier, all limits, retrieval/Epicure/embedding
settings, and scenario scripts. HEAD at review time is `c99a69c`, not
`5fd5300`. Keep `gpt-6-luna` and the agreed reasoning setting; do not change
reasoning just to populate diagnostic summaries. Keep web disabled for both
sessions, preferably at the operator switch too.

**$0.15 is a reasonable proposed campaign allowance, but it is not enforced
by `make demo`.** The agent loop has no USD guard and estimates input tokens
using characters/4. Thus the stated $0.12 for two sessions is an estimate,
not an unconditional worst-case bound. Current Standard short-context prices
confirm $0.10/M input and $0.50/M output; different context lengths, service
tiers and cache-write charges require the applicable rates.
[Source: official OpenAI pricing](https://developers.openai.com/api/docs/pricing).

Use one shared ledger across both sessions, model calls, embeddings, and any
authorized retry. Reuse/adapt the existing live-runner provider wrappers
where practical, test them with fakes, and include that runner in the freeze.
Reserve before dispatch, reconcile reported usage, retain uncertain charges,
and stop when the remainder cannot cover another call. Do not describe a
post-run cost report as a hard ceiling. This follows OpenAI's request-level
reservation approach.
[Source: official OpenAI spending-controller example](https://developers.openai.com/cookbook/articles/per_run_spending_controller_responses_api#check-the-budget-before-each-request).

Reconcile the unrecorded October 6–7 usage against available provider billing
and local logs before treating the old $0.28 milestone figure as current.
Record a separate H8 allowance within the remaining milestone budget.

Predeclare success for each complete workflow: clarification where needed,
recorded/resumed answers, suitable retrieved options, selected-dish plan,
and a grounded technique response or an honest coverage limitation. Review
quantity/ingredient associations, source/adaptation labels, and allergy
constraint handling in the transcript. A graceful budget-stop message is
useful recovery, but does not count as completing the requested plan.
Use a fresh session for each scenario; report first attempts separately.
Any code change requires a new freeze and separately labeled attempt.

**5. Push the reviewed branch; decide on merge after H8**

I recommend pushing the branch when authorized. There are **11 commits
ahead of the locally recorded upstream** at review time: the ten hardening
commits plus `c99a69c`, which added the decision documents. Remote state was
not refreshed during this review. Push the intended branch without forcing
unrelated refs. Git push updates the selected remote references; it is not
itself a merge into `main`. Whether a remote action deploys is a separate
repository configuration question.
[Source: Git push documentation](https://git-scm.com/docs/git-push).

Do not equate two H8 passes with Milestone 3 acceptance. Review the outcomes
and explicitly retain the known limits: ordinary-budget behavior, sparse
technique coverage, unresolved ranking work, and remaining corpus defects.

**Verification performed for this review**

- Read both decision documents, the rubric, H5 preview/procedure, H6 saved
  rankings/labels/scorer, and relevant loading/budget/cache code.
- Read the stored evidence for all 23 judgment rows with database-enforced
  read-only connections; checked the starting database counts.
- Verified the judgment-layer hash and independently counted pooled judgment
  coverage; rescored the proposed five additions in memory only.
- Ran `evals/phase7_agent/verify_packet.py`: 11 historical sessions verified,
  with 5 sufficient outcomes, 3 questions and 3 budget stops. This checks the
  packet's encoded claims; it is not a new live grounding evaluation.
- Did not rerun `make check` or the v10 harness. The reported 1,271 passing
  tests / 8 skips and 55/55 cases are the existing checkpoint evidence.
- No application writes, paid application calls, commits, pushes, or H8
  execution. Only this recommendations document was added.
