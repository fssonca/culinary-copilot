# H8 frozen-build live check: plan

Prepared 2026-10-08 under the Checkpoint D decisions
(`docs/phase7-owner-decisions.md`). The owner gave the go-ahead on
2026-10-08; the run uses the acknowledgement value below under the freeze
recorded here.

## What it checks

Two fresh sessions on one frozen build, with no code changes between
them, through the existing live runner (`evals/phase3_agent/live_run.py`):

| Scenario | Flow |
|---|---|
| `h8-party-baking` | vague baking request → questions answered in turn → options → select the first → plan → "how can I tell when it is done baking?" |
| `h8-allergy-dessert` | dessert for a friend with an unnamed allergy → question → "tree nuts" → checked options → select → plan → a cross-contact question |

Scenarios: `evals/h8_live/scenarios.json` (freeze sha256
`cb6ed064476dd8cf7b6b31c8e8b651bae548455143dde9215b068d02670ad1c5`).
Requests and answers are synthetic. Scripted answers go, in order, to
whatever the model asks, each at most once.

## Limits and settings

Per session, the `make demo` limits: 40 steps, 40 tool calls, 300k input
and 60k output tokens, 240 s per run. Web search off at the session
toggle and at the operator switch (`WEB_SEARCH_ENABLED=false`); the
runner's preflight refuses otherwise. Epicure on, embeddings on,
full-text retrieval by default, trajectory recording on. One attempt per
scenario (`--max-attempts 1`): first attempts only.

## Spending guard

The `h8` pool caps all H8 spend at **$0.15**, shared by both sessions and
every paid call (model turns, query embeddings, any authorized retry).
The runner reserves each call's worst case before sending it (input
bounded by the request's byte length plus overhead, output by the
maximum permitted), reconciles reported usage afterwards, keeps the
reservation when the outcome is ambiguous, refuses a call that does not
fit the remainder, and stops everything on a reservation breach. Spend
recorded in `data/h8-live/spend-history.json` by earlier H8 runs counts
against the same $0.15. `make demo` itself enforces no dollar ceiling;
only this runner does.

## Success, declared before the run

A scenario counts as complete only when all of these hold, judged from
its first attempt and its transcript:

1. **Clarification where needed.** The allergy scenario asks which
   allergen before offering options. The baking scenario may ask; its
   questions are judged for relevance.
2. **Answers recorded and used.** Each answer is stored and the next run
   uses it.
3. **Suitable options from retrieved recipes.** At least one option, all
   fetched in the session. Allergy: every option checked for tree nuts
   and none containing them.
4. **A plan for exactly the selected dish.** Quantities attached to the
   right ingredients; the source/adaptation label matches what the plan
   did.
5. **A grounded technique answer** citing technique chunks returned in
   the session, or an honest statement that the corpus does not cover
   the question.
6. **No budget stop.** A graceful budget-stop message is recovery, not
   completion.

The runner grades the mechanical parts (`task_completion`,
`workflow_complete`: options, plan and technique answer all reached;
`allergy_check`). Points 1, 3 (suitability), 4 and 5 also need
transcript review by the owner.

Two complete sessions show these workflows working under demo limits on
this build. They do not establish general reliability, behaviour under
the ordinary limits, or Milestone 3 acceptance.

## Before the run

1. Owner go-ahead for the H5 cleanup, then its application run
   (`docs/h5-summary-cleanup.md`), so H8 sees the cleaned corpus.
2. Reconcile the unrecorded 2026-10-06/07 usage against provider billing
   and record the remaining Milestone 3 budget; the $0.15 must fit in it.
3. Freeze: fill in the record below and commit it. Any later code change
   means a new freeze and a separately labelled attempt.

## Freeze record (2026-10-08)

Recorded by an AI assistant at the owner's go-ahead; not a signature.

| Item | Value |
|---|---|
| Commit | Code as of `05f20ca`. The freeze commit that records this table changes only docs and H6 result files; `git diff 05f20ca -- src scripts evals/phase3_agent evals/h8_live/scenarios.json` is empty. |
| Corpus | After the H5 cleanup: 15,875 recipes, 644 quarantine rows, 3 imports (last `foodie-repair-v5-20261008`), 14,659 foodie embedding rows, 0 `summary` titles |
| Model, reasoning effort, service tier | `gpt-6-luna`, `LLM_REC_REASONING_EFFORT=none`, standard tier (registry prices verified 2026-09-24) |
| Limits | As in the scenarios file: 40 steps, 40 tool calls, 300k input and 60k output tokens per session |
| Retrieval, Epicure, embeddings | `RETRIEVAL_MODE=fulltext`, technique retrieval fulltext, `EPICURE_ENABLED=true`, `EMBEDDINGS_ENABLED=true` (`text-embedding-3-small`), `WEB_SEARCH_ENABLED=false` |
| Scenarios freeze sha256 | `cb6ed064476dd8cf7b6b31c8e8b651bae548455143dde9215b068d02670ad1c5` (over the file without its `freeze_sha256` field; checked on load) |
| H8 spend before the run | $0.00 (no `data/h8-live/spend-history.json`) |
| Budget | The owner authorized $1 for this session's live calls; the H8 pool is capped at $0.15 within it. The 2026-10-06/07 usage is not reconciled against provider billing (owner action, still open). |

## Command (after the go-ahead)

```
HF_HUB_OFFLINE=1 LLM_RECOMMENDATION_ENABLED=true EPICURE_ENABLED=true \
EMBEDDINGS_ENABLED=true WEB_SEARCH_ENABLED=false \
uv run python evals/phase3_agent/live_run.py --live --yes \
  --budget-pool h8 --ceiling-usd 0.15 \
  --acknowledge-live-run h8-checkpoint-d-2026-10-08 \
  --scenarios-file evals/h8_live/scenarios.json --max-attempts 1 \
  --expect-db-name culinary_copilot --expect-db-host localhost \
  --raw-dir data/h8-live/raw --summary-out data/h8-live/summary.json
```

Raw responses, trajectories and the summary stay under `data/h8-live/`
(not committed). The committed report paraphrases model output and never
quotes it.

## Dry run (done 2026-10-08)

The same command with `--fake` (scripted model, disposable database
`culinary_test_h8_fake`): both scenarios completed, workflow complete,
isolation ok, $0.00. Tests: `tests/test_h8_live_pool.py`.

## Result (2026-10-08)

First attempt: 0 of 2 complete; both sessions hit the repeat-stall stop
(`agent_no_progress`), $0.0143 spent. Details, two grading bugs found
and fixed, and the proposed next step: `RESULTS.md`. Any further attempt
is a separately labelled attempt on a new freeze with
`scenarios_v2.json`.

## Second attempt: freeze record (2026-10-08)

Recorded by an AI assistant at the owner's go-ahead; not a signature.
The owner agreed with the recommendations in `RESULTS.md` (stall
recovery, then a separately labelled attempt on fresh scenarios) and set
the H8 pool to $1.00.

| Item | Value |
|---|---|
| Label | H8 attempt 2; acknowledgement `h8-attempt-2-2026-10-08` (the attempt-1 value is refused) |
| Commit | Code as of `91c5a71` (stall recovery, grading fixes, $1.00 pool). The commit that records this table changes only docs. |
| Corpus | Unchanged since attempt 1: 15,875 recipes, 644 quarantine rows, 3 imports (last `foodie-repair-v5-20261008`), 0 `summary` titles |
| Model, reasoning effort, service tier | `gpt-6-luna`, `LLM_REC_REASONING_EFFORT=none`, standard tier |
| Limits | Demo limits, as in the scenarios file (40 steps, 40 tool calls, 300k/60k tokens, 240 s per run) |
| Retrieval, Epicure, embeddings | `RETRIEVAL_MODE=fulltext`, technique retrieval fulltext, `EPICURE_ENABLED=true`, `EMBEDDINGS_ENABLED=true`, `WEB_SEARCH_ENABLED=false` |
| Scenarios | `scenarios_v3.json`, freeze sha256 `9c09c6930238e67a958ac0e8cf90815744ffdf6c5ab0c72681dd138d493de16a`: `h8b-bake-sale`, `h8b-allergy-treat` (peanuts), written before this run |
| Harness | Cases v11, 58/58 |
| Budget | H8 pool $1.00 including attempt 1's $0.0143: $0.9857 available |
| Success | Unchanged from "Success, declared before the run" above |

Command: the attempt-1 command with `--ceiling-usd 1.00`,
`--acknowledge-live-run h8-attempt-2-2026-10-08`,
`--scenarios-file evals/h8_live/scenarios_v3.json`,
`--raw-dir data/h8-live/raw-attempt-2` and
`--summary-out data/h8-live/summary-attempt-2.json`.

## Second attempt result (2026-10-08)

0 of 2 complete; both sessions got further than attempt 1 and the stall
recovery worked in both. The allergy session's finishing options were
rejected by a validator false positive (fixed after the run); the bake
sale's follow-up re-issued the plan instead of a technique answer.
$0.0423 spent, H8 total $0.0566 of $1.00. Details: `RESULTS.md`,
"Second attempt".

## Third attempt: freeze record (2026-10-08)

Recorded by an AI assistant at the owner's go-ahead ("proceed with your
recommendations, and do a new attempt"); not a signature.

| Item | Value |
|---|---|
| Label | H8 attempt 3; acknowledgement `h8-attempt-3-2026-10-08` (earlier values refused) |
| Commit | Code as of `5447fac`: attempt-2 fixes (answers support note claims, `692b12c`) and the follow-up framing line. The commit that records this table changes only docs. |
| Corpus | Unchanged: 15,875 recipes, 644 quarantine rows, 3 imports (last `foodie-repair-v5-20261008`), 0 `summary` titles |
| Model, reasoning effort, service tier | `gpt-6-luna`, `LLM_REC_REASONING_EFFORT=none`, standard tier |
| Limits | Demo limits, as in the scenarios file |
| Retrieval, Epicure, embeddings | `RETRIEVAL_MODE=fulltext`, technique retrieval fulltext, `EPICURE_ENABLED=true`, `EMBEDDINGS_ENABLED=true`, `WEB_SEARCH_ENABLED=false` |
| Scenarios | `scenarios_v4.json`, freeze sha256 `7d01af3abd5bdb9945fa223145e362c9c1aec5d708a10f5c6e438509575550f5`: `h8c-office-birthday`, `h8c-allergy-sleepover` (eggs), written before this run |
| Harness | Cases v12, 59/59 |
| Budget | H8 pool $1.00; $0.0566 spent by attempts 1 and 2; $0.9434 available |
| Success | Unchanged from "Success, declared before the run" above |

Command: the attempt-2 command with
`--acknowledge-live-run h8-attempt-3-2026-10-08`,
`--scenarios-file evals/h8_live/scenarios_v4.json`,
`--raw-dir data/h8-live/raw-attempt-3` and
`--summary-out data/h8-live/summary-attempt-3.json`.

## Third attempt result (2026-10-08)

1 of 2 complete (`h8c-allergy-sleepover`: options checked for egg, plan,
cited technique answer). The office-birthday plan was rejected by a
quantity-check false positive, fixed after the run. $0.0319 spent, H8
total $0.0885 of $1.00. Details: `RESULTS.md`, "Third attempt".

## Fourth attempt: freeze record (2026-10-08)

Recorded by an AI assistant at the owner's go-ahead ("proceed with your
recommendations, do the new attempt"); not a signature.

| Item | Value |
|---|---|
| Label | H8 attempt 4; acknowledgement `h8-attempt-4-2026-10-08` (earlier values refused) |
| Commit | Code as of `3ec6beb`: attempt-3 fix (list quantity attribution, `8bb1449`) and the selected recipe in the plan run's input. The commit that records this table changes only docs. |
| Corpus | Unchanged: 15,875 recipes, 644 quarantine rows, 3 imports (last `foodie-repair-v5-20261008`), 0 `summary` titles |
| Model, reasoning effort, service tier | `gpt-6-luna`, `LLM_REC_REASONING_EFFORT=none`, standard tier |
| Limits | Demo limits, as in the scenarios file |
| Retrieval, Epicure, embeddings | `RETRIEVAL_MODE=fulltext`, technique retrieval fulltext, `EPICURE_ENABLED=true`, `EMBEDDINGS_ENABLED=true`, `WEB_SEARCH_ENABLED=false` |
| Scenarios | `scenarios_v5.json`, freeze sha256 `cbbc6126ceb63e604eda19e419cc57e0cd023d9769c61b706cf65f4c560b1db3`: `h8d-family-picnic`, `h8d-allergy-potluck` (tree nuts), written before this run |
| Harness | Cases v13, 59/59 |
| Budget | H8 pool $1.00; $0.0885 spent by attempts 1 to 3; $0.9115 available |
| Success | Unchanged from "Success, declared before the run" above |

Command: the attempt-3 command with
`--acknowledge-live-run h8-attempt-4-2026-10-08`,
`--scenarios-file evals/h8_live/scenarios_v5.json`,
`--raw-dir data/h8-live/raw-attempt-4` and
`--summary-out data/h8-live/summary-attempt-4.json`.

## Fourth attempt result (2026-10-08)

1 of 2 complete (`h8d-family-picnic`: options, a plan on the plan run's
first turn, a cited technique answer). The potluck plan was rejected by
an equipment-size false positive (fixed after the run), then by the
fidelity check. $0.0249 spent, H8 total $0.1133 of $1.00. Details:
`RESULTS.md`, "Fourth attempt".
