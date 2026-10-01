# Phase 3 live evaluation: review (2026-09-30)

Written with AI assistance from the runner summaries and the raw
trajectories in `data/phase3-live/` (gitignored). The owner started
every paid run. This document records results and findings; it does not
sign checkpoint A for the owner (see "Owner decision" below).

Model output is summarised here rather than quoted, in line with
AGENTS.md (model responses are not committed).

## Setup

- **Model:** `gpt-6-luna` (pricing `2026-09-24-luna-v1`).
  **Query embeddings:** `text-embedding-3-small`.
- **Database:** option A (application database) with verified trial
  isolation. Every attempt uses a fresh session, and isolation was OK on
  every run.
- **Scenarios:** `live_scenarios_v2.json`, owner-approved 2026-09-30.
  v1 is kept byte-identical; see LIVE_PLAN for the diff.
- **Epicure:** enabled per scenario, with a cache-only probe in
  preflight. `live-epicure-unavailable` disables it through
  per-scenario settings only.
- **Budget:** $0.15 cap with pre-call reservations. The cumulative
  ledger is in `data/phase3-live/spend-history.json`.

## Run history

| Attempt | Outcome | Cause, and fix before the next run | Recorded spend |
|---|---|---|---|
| 1 | Crash before any call | `build_tool_context` was called positionally | $0 |
| 2 | Refused | Recommendation generation disabled; preflight now requires it | $0 |
| 3 | 400 on the first turn | Tool and response schemas broke strict mode; retry setting not zeroed; error detail dropped | $0.0035 (kept as ambiguous) |
| 4 | 2 ended, 1 stopped on a 400 | The user's request never reached the model; the history cap split a call from its output; scripted answer used a fake question id | $0.0059 |
| 5 | 0 answers | `EPICURE_ENABLED=false` inherited from `.env`; the model kept retrying the unavailable Epicure tools | $0.0239 |
| 6 | 1 of 8 answered | No forced final turn; Epicure vocabulary misses; no answer shape for technique or pairing questions; phase error was terminal | $0.0331 |
| 7 | 6 of 8 answered | No plan after select; hard-constraint key contract not stated; Epicure lines not required; answers weakly relevant to requests | $0.0269 |
| 8 | 7 of 8 answered, all 7 matched | Dropped-option feedback was garbled (dict keys iterated); stale note after drops | $0.0206 |
| 9 | 4 of 4 answered (targeted rerun) | none | $0.0110 |

**Total recorded: $0.1249 of $0.15.** This figure is conservative: the
two rejected requests (400s) are counted at their reserved cost, and
were probably not billed.

## Final results (v2 scenarios)

The final set combines two runs:
- attempt 9 for chicken dinner, yogurt, lentil and roast pairing, rerun
  after the dropped-option fix;
- attempt 8 for the other four. None of those four dropped an option,
  so the later fix does not change them.

**This is structural success, not a quality result.** All 8 reached
their expected stop and grade `task_completion: true`. That grade
checks only the answer's shape:
- the expected stop;
- the option count;
- whether a plan or technique answer is present;
- the Epicure line minimum.

It does not judge answer quality, relevance, or whether hard
constraints hold at ingredient level. The per-scenario notes below are
a manual review of 8 cases, not a quality evaluation.

**About first attempts:** these results come from the last run of each
scenario, after several rounds of fixes. The same scenarios failed in
attempts 3–7. Attempt 9 ran with `--max-attempts 1`, so no retry was
possible. "First attempt" here means only that no in-run retry was
needed. It is not a first-try success rate for the agent.

| Scenario | Result | Review notes |
|---|---|---|
| Chicken dinner, 30 min | 2 options, select, then a plan for *Quick Air Fryer Chicken Parmesan* | The plan was rejected once (empty fields), then accepted. The source record has ingredients only, so the steps rely on package directions and are labelled as an adaptation. The note says honestly that the cooking time is not confirmed. |
| Yogurt ("might have yogurt") | 3 chicken-and-yogurt options (two gyros, curried chicken salad) | It did not ask about yogurt, so the scripted answer was never used. The note acknowledges yogurt is uncertain, but no yogurt-free fallback was offered. |
| Direct lentil | *Vegan Turkish Red Lentil Soup* | Two further lentil soups were dropped for invented quantities ("to taste" given an amount). The server note replaced the model note, which described three options. |
| Vegetarian, hearty | 2 lentil stews | `dietary_constraints` was honored. The note flags checking whether the vegetable bouillon is vegetarian. No ingredient-level check exists yet. |
| Dragonfruit soufflé glacé (no match) | Asks one concrete question offering a similar frozen dessert | Matches the owner's checkpoint A q1 note. |
| Epicure unavailable | 2 roast chicken options, degraded | An invented technique ref was rejected first. The skip reason is `epicure_not_configured` and the answer is marked degraded. |
| Technique: boiling an egg | `technique_answer` citing 2 chunks of `tech-egg-boil-18` | Correct timing guidance with the CC BY-SA attribution attached. Epicure skip `simple_technique_question`. |
| Roast pairing | 3 roast chicken recipes plus Epicure lines | The model rejected pork and beef and used chicken broth, each with a reason. The note's "classic companions" (lemon, garlic, rosemary, potatoes) come from model knowledge, not Epicure. |

## Checkpoint A reservations, checked live

- **Stops:**
  - Final runs stop on sufficient evidence, or ask when nothing matches.
  - Budget stops fire correctly (seen in attempts 5–7).
  - The run-level stops (consecutive failures, contact-operator, budget)
    ended three bad runs early.
- **Evidence and grounding (P3-A-01):**
  - Every option in every final was retrieved in its session, and plans
    cite a `get_recipe` document.
  - Quantities are copied from the source, and invented ones are
    dropped.
- **Epicure by default (P3-A-02):**
  - Consulted in every scenario except the two allowed skips (technique
    question, unavailable Epicure).
  - Epicure lines are now required and shown.
- **Adaptations:** labelled (the chicken plan steps, and attempt 7's
  dessert adaptation).
- **Request relevance:** acceptable in the final runs. Weak cases:
  - attempt 7 offered a coffee semifreddo for the dragonfruit request
    (fixed by framing);
  - the yogurt scenario offers no yogurt-free alternative.
- **First attempt vs retry:** not established. The final runs needed no
  in-run retry, but they followed code fixes, and attempt 9 allowed no
  retry. In attempts 5–7, retries mostly repeated the same failure.
- **Ask and resume:** not validated live (P3-L-13). The yogurt scenario
  never asked. The dragonfruit scenario asked, but its flow has no
  scripted resume. No live run has recorded an answer and then resumed
  the session.

## Findings

| ID | Finding | Status |
|---|---|---|
| P3-L-01 | Strict-mode schema violations (tools, `PlanSource`) | Fixed |
| P3-L-02 | The user's request was never sent to the model (API had no message field) | Fixed (`user_message` event) |
| P3-L-03 | The history cap split call/output pairs | Fixed, with a pre-send check |
| P3-L-04 | The model never converged (no final turn; Epicure vocabulary misses; no answer shapes) | Fixed |
| P3-L-05 | Garbled dropped-option feedback; inconsistent partial acceptance; stale note | Fixed |
| P3-L-06 | `constraints_honored` held unverifiable free text, and one claim was false (attempt 7) | Fixed (keys only); free-text notes stay unverified |
| P3-L-07 | Options are not checked at ingredient level against hard constraints (vegetarian) | Open: minimum control, offline, before Phase 5 |
| P3-L-08 | The note can carry unsourced claims (pairing "classics", timing) | Open: minimum claim-grounding control, offline, before Phase 5 |
| P3-L-09 | Plans for ingredient-only records are model-written steps; raw-poultry plans need food-safety technique refs | Open: minimum plan-evidence control, offline, before Phase 5; quality cases (tq-04/tq-15) in Phase 7 |
| P3-L-10 | The note is cut at 280 characters (`_NOTE_LIMIT`) while the schema allows 2000; tool outputs are cut at 4000 characters of serialized JSON | Open: truncation controls, offline, before Phase 5 |
| P3-L-11 | Epicure `balanced` pairings for chicken are weak (pork, beef, peanut); cooc gives more useful ones | Deferred to Phase 7 (Epicure comparison) |
| P3-L-12 | Session budgets (8 steps, 12 tool calls, 30k input tokens) leave little room for recommend and plan together | Deferred to Phase 7 (session-budget tuning) |
| P3-L-13 | Ask-and-resume has not been validated live | Open: stays open until a live run records an answer and resumes |
| P3-L-14 | Input-token reservations under-estimate: 16 model turns in attempts 6–9 used more input than reserved (up to 1.17×). No turn exceeded its dollar reservation, only because the output reservation dominates | Open: fix before any further paid run |

Broader quality evaluation (relevance, answer quality, claim
consistency at scale) is deferred to Phase 7.

## Owner decision (2026-09-30)

The owner's decision, recorded verbatim:

> Accept the live evaluation as a diagnostic with reservations. Correct
> the structural-success and first-attempt wording, and keep live
> ask-and-resume validation open. Address minimum constraint,
> claim-grounding, plan-evidence, and truncation controls offline before
> Phase 5. Defer broader quality evaluation, Epicure comparison, and
> session-budget tuning to Phase 7. Fix spending reservations before any
> further paid run. Keep the remaining budget unspent for now.

What this means:
- **The live evaluation is accepted as a diagnostic only**, not as
  evidence of answer quality. The wording above has been corrected
  accordingly.
- **P3-L-13 stays open.**
- **Offline before Phase 5:** P3-L-07, P3-L-08, P3-L-09, P3-L-10 and
  P3-L-14.
- **Deferred to Phase 7:** P3-L-11, P3-L-12 and the broader quality
  evaluation.
- **No further paid run** until P3-L-14 is fixed and the owner gives an
  explicit go-ahead. The remaining budget (about $0.025) stays unspent.

The decision is recorded in `owner_review.json` under
`live_evaluation.result`.
