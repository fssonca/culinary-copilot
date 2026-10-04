# Phase 7 live-run plan (part 2, PREPARED ONLY — DO NOT RUN)

Owner authorization required. No live call has been made from this
plan. The runner refuses to run the `phase7` pool without an explicit
acknowledgement flag (`--acknowledge-live-run`), and every limiting
flag is enforced inside the run, not only at preflight (tested
offline; see the enforcement table).

## Checkpoint C questions this answers

- Are claims supported? (options cite retrieved recipes with exact
  quantities; plans cite the selected recipe plus food-safety refs;
  web answers cite real pages.)
- Are constraints ever relaxed? (vegan case and the peanut-resume
  path, with no silent relaxation.)
- Are the search citations real and relevant? (permission-on web
  answer whose ref comes from provider evidence; per-session limit
  enforced; permission-off gate holds.)

## What the runner actually drives

The runner drives the agent loop (`agent/loop.py`) and the Postgres
session store directly — the same code the API endpoints call — not
HTTP, not SSE, and not the `/ui` page. "Select then plan" runs the
runner's `select-first` step (`record_select`) followed by a `plan`
run; "question then answer then resume" runs the `resume` step
(`record_answer` on the actual pending question, then a resumed run);
the search toggle is covered as permission off from the start (the
mid-session toggle has no runner support and belongs to the manual UI
check below).

## Scenarios

`evals/phase7_agent/live_scenarios.json`
(version `live-scenarios-phase7-2026-10-04`, synthetic requests,
runner schema: key, request, session, settings, scripted_answers,
flow, fake_flow, expected; flows use the runner's `select-first` and
`resume` steps; search on for `live-search-once`, off for
`live-search-toggle`):

- file sha256: `f202afb67cc0f01b18b588f38a3884eaf3eb20da6528c2c8139f399ea5d0577d`
- freeze sha256 (`load_scenarios` canonical-body check):
  `5d9e17a540b75c3273b90d4ffe26b55c8170cae041d4ff170f80f857491d162e`

| Key | Flow | Fake flow | Expected stop |
|---|---|---|---|
| live-chicken-e2e | recommend, select-first, plan | full-requery | sufficient, plan |
| live-yogurt-ask | recommend-ask, resume | ask | sufficient, asked + recorded + resumed |
| live-peanut-allergy | recommend-ask, resume | ask-allergy | sufficient, allergy check |
| live-vegan-conflict | recommend | direct | sufficient, min 1 option |
| live-search-once | recommend | web-discovery | sufficient, web answer, no question |
| live-search-toggle | recommend | direct | sufficient, search not offered, no web answer |
| live-plan-safety | recommend, select-first, plan | full-requery | sufficient, plan |

`full-requery` is the `full` fake with the plan run re-querying
techniques first, which is what a real model does after select
(technique refs must be returned in the plan run's own invocation).
The toggle scenario holds permission off from the start; the fake
`direct` script then answers from the local corpus.

## Exact command (after explicit owner "run it")

```sh
uv run python evals/phase3_agent/live_run.py --live --yes \
  --budget-pool phase7 \
  --acknowledge-live-run phase7-checkpoint-c-2026-10-04 \
  --acknowledge-search-estimate phase5-decision-4-2026-10-02 \
  --scenarios-file evals/phase7_agent/live_scenarios.json \
  --scenarios live-chicken-e2e,live-yogurt-ask,live-peanut-allergy,live-vegan-conflict,live-search-once,live-search-toggle,live-plan-safety \
  --ceiling-usd 0.15 --max-attempts 1 \
  --search-max-per-live-session 1 --max-campaign-searches 1 \
  --expect-db-name <owner-live-db> --expect-db-host <owner-live-host> \
  --raw-dir data/phase7-live/raw \
  --summary-out data/phase7-live/live-summary-phase7.json
```

`--expect-db-name` / `--expect-db-host` name the owner's live
database; fill them before running. `--yes` is required by `main`
alongside `--live --ceiling-usd`. Removed flags that do not exist:
`--max-model-turns`, `--max-searches-per-scenario`,
`--campaign-search-cap`, `--search-estimate-usd`,
`--technique-compare`. The campaign cap is the per-pool constant
(`PHASE7_CAMPAIGN_SEARCH_CAP = 5`), not a flag.

## Enforcement (flag → in-run check → offline test)

- `--ceiling-usd 0.15` (pool cap $0.50, a proposal for the owner in
  checkpoint C, not a decision): `main` refuses a ceiling above the
  pool cap (`live_run.py:2872`); the run ledger starts at ceiling
  minus recorded prior (`live_run.py:2955`); every model turn reserves
  input plus maximum output via `SpendLedger.reserve`
  (`live_run.py:533`) called by `LedgerModelProvider` (`live_run.py:909`),
  and unaffordable turns stop the scenario as budget-exhausted.
  Tests: `test_phase7_refuses_ceiling_above_cap`,
  `test_phase7_ledger_refuses_over_cap`.
- `--acknowledge-live-run`: preflight refuses any other value
  (`live_run.py:1252`). It is an authorization, enforced at
  preflight by nature. Tests: `test_phase7_refuses_without_ack`,
  `test_phase7_accepts_with_ack`.
- `--acknowledge-search-estimate`: preflight refuses live search runs
  without the exact value (`live_run.py:1205`); search spend itself is
  bounded in-run (next rows). Tests:
  `test_preflight_ack_required_when_search_selected`,
  `test_preflight_ack_rejects_wrong_value` (`tests/test_phase5_runs.py`).
- `--search-max-per-live-session 1`: built into `SearchRunLimits`
  (`live_run.py:2943`), attached to the tool context
  (`live_run.py:2461`), enforced per dispatch inside `search_web`
  after the slot claim (`src/culinary_copilot/tools/stub_tools.py:233`).
  Tests: `test_phase7_search_cap_enforced_inside_run`,
  `test_session_limit_allows_one_of_three`.
- `--max-campaign-searches 1` and the pool campaign cap (5):
  `SearchRunLimits.check_and_claim` refuses at the run limit and at
  prior-plus-dispatched (`live_run.py:427`), counted synchronously at
  dispatch; preflight refuses when prior spend already reaches the cap.
  Tests: `test_search_run_limits_check_and_claim`,
  `test_search_run_limits_campaign_cap_counts_prior`,
  `test_preflight_refuses_when_campaign_cap_reached`.
- Search estimate: reconciled against the $0.025 estimate in-run;
  overrun raises `SearchEstimateExceeded` and stops the campaign at
  once (`live_run.py:2616`); preflight refuses while a breach is
  unacknowledged. Tests: `test_ledgered_search_over_estimate_stops`,
  `test_preflight_refuses_unacknowledged_estimate_breach`.
- `--max-attempts 1`: the scenario loop runs at most one attempt
  (`live_run.py:2448`); provider-internal retries are forced to zero
  (`live_run.py:1076`).
- `--expect-db-name` / `--expect-db-host`: `main` requires both
  (`live_run.py:2875`); preflight refuses a mismatch
  (`live_run.py:1296`).
- `--scenarios-file` / `--scenarios`: `load_scenarios` verifies the
  freeze hash (`live_run.py:883`); `tests/test_phase7_live_plan.py`
  parses this exact command block with the runner's own parser and
  checks every key against the scenario file, so the plan cannot drift
  from the code.

## Estimates

Per-turn reservation ($0.0036, the ceiling basis): the runner reserves
each model turn at its upper bound — `estimate_request_tokens`
(byte length of the serialized input items, tools array and text
schema plus overhead, `live_run.py:269`) of priced input plus the full
per-turn maximum output (`llm_rec_max_output_tokens = 6500`,
`config.py:82`). At gpt-6-luna prices ($0.10/1M input, $0.50/1M
output, `llm/models.py:58`): 6500 output tokens reserve $0.00325 and a
typical ~2–3k-token agent-turn input reserves ~$0.0002–0.0003, rounded
up to the $0.0036 planning figure. Search reservation $0.025 per
dispatch (decision 4, option A estimate, acknowledged overrun risk).

Actual reconciled costs (expected-cost basis): 40 model turns across
`data/phase3-live` (14 turns, $0.0055) and `data/phase5-live` (26
turns, $0.0098) reconcile to a mean of ~$0.00038 per turn and a max of
~$0.00054; 6 reconciled searches total $0.0667, mean ~$0.0111 per
search; embeddings negligible ($0.000001 over 4 calls).

Totals for the 7 scenarios (25 turns, 1 search):

| Basis | Turns | Searches | Total |
|---|---|---|---|
| Reservation (ceiling) | 25 × $0.0036 = $0.0900 | 1 × $0.025 = $0.0250 | **$0.1150** |
| Expected (actuals) | 25 × $0.0004 ≈ $0.0100 | 1 × $0.011 ≈ $0.0110 | **≈ $0.0210** |

`--ceiling-usd 0.15` covers the $0.1150 reservation with margin, under
the proposed $0.50 pool cap. Remaining Milestone 3 budget: about $0.77
of $1.00 ($0.1304 + $0.1015 recorded).

## Optional owner manual UI check (NOT ledger-covered)

Optionally, after the runner completes, the owner may start the server
with `LLM_RECOMMENDATION_ENABLED=true` in the server-start environment
(config default false; set at start, never in `.env` by this plan) and
use `/ui` for a few messages, including the search toggle and one
permission-on search. This exercises HTTP/SSE and the page, which the
runner never touches — and it is NOT covered by the runner's ledger,
so its spend must be recorded by hand.

Worst case per UI session, from the session budgets (30k input /
12k output tokens, at most 3 code-limit searches): 30000/1e6 × $0.10
+ 12000/1e6 × $0.50 + 3 × $0.025 = $0.003 + $0.006 + $0.075 =
**$0.084**. A few messages stay far below this; record the actual
usage from the server logs by hand.

## Open items (not in this run, no runner support)

- Technique-mode comparison (full-text vs vector on tq-04, tq-15 and
  the agent's own choice): the runner has no mode-compare path, so it
  is removed from this run. It would need a new runner flow (paired
  runs with per-mode tool contexts), query embeddings
  (`EMBEDDINGS_ENABLED` plus a pgvector database with migration 007
  rows), and its own estimate. Not built here.

## Fake dry run (offline, disposable DB, no model calls)

2026-10-04, `--fake` on disposable `culinary_check_phase7dry`
(created and dropped by the run; never `culinary_copilot`):
7 answered, 0 no-answer, 0 stopped, 0 not_run of 7; 7 matched
expected stop; isolation ok True; spent $0.0000.

| Scenario | Status | Stop |
|---|---|---|
| live-chicken-e2e | completed: answered | agent_sufficient_evidence |
| live-yogurt-ask | completed: answered | agent_sufficient_evidence |
| live-peanut-allergy | completed: answered | agent_sufficient_evidence |
| live-vegan-conflict | completed: answered | agent_sufficient_evidence |
| live-search-once | completed: answered | agent_sufficient_evidence |
| live-search-toggle | completed: answered | agent_sufficient_evidence |
| live-plan-safety | completed: answered | agent_sufficient_evidence |

Do not run the live command from this plan without explicit owner
authorization.
