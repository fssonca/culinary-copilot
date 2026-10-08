# H8 frozen-build live check: results

Run 2026-10-08 (21:24 UTC) after the owner's go-ahead, under the freeze
in `PLAN.md` (code `05f20ca`, cleaned corpus, scenarios v1
`cb6ed064…`). First attempts only, one attempt per scenario. Model
output is paraphrased here, never quoted; the raw records stay in
`data/h8-live/` (not committed).

## Outcome: 0 of 2 complete

| Scenario | Reached | Stopped at | Stop |
|---|---|---|---|
| `h8-party-baking` | options (3 fetched cakes and desserts, no question asked) | plan run, step 4 | `agent_no_progress`: third identical `get_recipe` call |
| `h8-allergy-dessert` | question about the allergen, answer "tree nuts" recorded | resumed options run, step 8 | `agent_no_progress`: third identical `find_balanced_pairings` call |

Neither session reached a plan or a technique answer, so neither meets
the predeclared criteria. No budget stop, no provider error, no isolation
problem. The runner stopped after the second scenario on its
consecutive-failures rule; both scenarios had already run.

Against the predeclared points: (1) the allergy session asked for the
allergen before offering options; the baking session offered options
without asking, which the plan allows. (2) The allergy answer was
recorded and the next run used it. (3) Baking options came from recipes
fetched in the session; the allergy session produced none. (4), (5) not
reached. (6) no budget stop.

## What happened

**Party baking.** Discovery went normally: a five-word full-text search
returned nothing, the model switched to vector searches, fetched four
recipes and finished with three options. After "select the first" the
plan run offered `get_recipe`, `search_techniques` and `convert_units`.
The model fetched the selected recipe, then fetched it again with the
same arguments. That step held only a repeat, so the wrap-up turn
withheld `get_recipe` and told the model to finish; it ran a technique
search instead. On the next turn `get_recipe` was offered again and the
model fetched the same recipe a third time, which is the stall stop. It
never attempted a plan. The recipe output it had was complete: 3
ingredients and all 4 directions, nothing truncated, no ranged read
needed (checked offline against the stored record).

**Allergy dessert.** The model asked which allergen, the scripted
"tree nuts" answer was recorded, and the resumed run searched and
fetched five recipes. It called `find_balanced_pairings` for chocolate
with identical arguments at steps 1, 6 and 8. The second call triggered
the wrap-up (one turn without the tool, which the model spent on another
fetch); the third was the stall stop.

Both failures are the same pattern: the repeat wrap-up withholds the
repeated tool for one turn, the tool comes back, the model repeats the
call a third time, and the stall guard ends the run. The guard did its
job (no runaway spend), but the session ends with nothing for the user
instead of finishing on the evidence already gathered. The model ran at
reasoning effort `none`, emitting only a tool call per turn (26 to 148
output tokens).

Smaller observations, not causes of the stops: each session's first
full-text search used several words and returned nothing; each session
once passed the dataset id as the source id (rejected as invalid
arguments); one pairing call used "cake" (rejected).

## Grading bugs found and fixed

Reviewing the grades exposed two bugs in the H8 grading added in
`05f20ca`. Neither changed this run's verdict (no session reached
options-then-plan, and the allergy session produced no options):

1. The allergy grade checked option ingredients for **peanuts only**; a
   tree-nut option would have passed. The scenario now names its
   allergen terms (`expected.allergen_terms`, a list kept in the scenario
   rather than reused from the app's checker); older peanut scenarios
   keep their keys.
2. Options were read from the **last run only**. An H8 session ends on
   the technique follow-up, which carries no options, so even a complete
   allergy session would have failed `allergy_pass` with nothing checked.
   Workflow scenarios ending on a technique answer now grade the
   session's stored options.

Fixed in `evals/phase3_agent/live_run.py` with tests in
`tests/test_h8_live_pool.py`. The scenarios with the allergen terms are
`scenarios_v2.json` (freeze `95f024c7…`); `scenarios.json` (v1) stays
frozen as the record of this attempt.

## Spend

$0.0143 of the $0.15 pool: 23 model turns (133,353 input and 1,875
output tokens) and 6 query embeddings, all reconciled; the largest
single reservation was $0.0072. $0.1357 remains in the pool. Recorded in
`data/h8-live/spend-history.json`.

## What this shows

On this build and these two scenarios, under demo limits, both
workflows failed on their first attempt for the same reason. It is one
sample per scenario; it says nothing about general reliability in either
direction.

## Proposed next step (owner decision)

1. **Loop change.** After a stall, give the model one tool-less
   finishing turn instead of stopping outright, the same recovery H4
   uses after a budget excess: it must finish from the evidence it has
   (options, plan) or ask one question; a tool call on that turn still
   stops the run. Alternatively, keep the repeated tool withheld for the
   rest of the run once the wrap-up has fired. Either needs offline tests
   and harness cases first.
2. **A second attempt**, labelled separately from this one, on a new
   freeze with `scenarios_v2.json`. The same scenarios would then be
   both the motivation for the change and its check, so a pass would be
   weak evidence; fresh scenarios written before the run would be
   stronger. Expected cost about $0.02, within the $0.1357 left.
