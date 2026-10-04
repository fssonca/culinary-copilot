# Phase 7 owner decisions

Owner decisions for Milestone 3, Phase 7 (scenario evals and the bounded
live run). Each entry is recorded by an AI assistant and is not a
signature.

## Live run authorization (2026-10-04)

Recorded by an AI assistant; not a signature. The owner approved:

- **Run the Phase 7 live check** as prepared in
  `evals/phase7_agent/LIVE_PLAN.md`:
  - 7 scenarios from `evals/phase7_agent/live_scenarios.json`
    (sha256 `f202afb6…`);
  - one attempt each;
  - at most 1 web search in the whole run
    (`--search-max-per-live-session 1 --max-campaign-searches 1`);
  - hard run ceiling **$0.15**;
  - expected cost about $0.02;
  - run against the application database (`culinary_copilot` on
    `localhost`), as in the Phase 3 and Phase 5 live runs.
- **Phase 7 pool cap: $0.50** of the $1.00 Milestone 3 ceiling.
  Phase 3 used $0.1304 and Phase 5 $0.1015 before this run.
- **Commit Phase 7 part 1 first**, so the code that runs is recorded.

Known gap, accepted for this run: preflight checks for unacknowledged
search-estimate breaches only in the Phase 5 history, not in the
Phase 7 one. The in-run breach stop applies. Fix before any further
Phase 7 live run.

## Live run results (2026-10-04)

Run by an AI assistant at the owner's request, with the command from
`evals/phase7_agent/LIVE_PLAN.md` plus the environment
`HF_HUB_OFFLINE=1 LLM_RECOMMENDATION_ENABLED=true EPICURE_ENABLED=true`.
The first attempt was refused at preflight (`HF_HUB_OFFLINE` unset),
with nothing spent.

- **Spend:** $0.0292 of the $0.15 run ceiling, with 1 web search.
  Milestone 3 total is about $0.2611 of $1.00 (Phase 3 $0.1304,
  Phase 5 $0.1015, Phase 7 $0.0292).
- **Matched the expected stop (3 of 7):**
  - live-search-once: a cited web answer from provider evidence;
  - live-search-toggle: local options, no search;
  - live-yogurt-ask: options.
- **Did not match (4 of 7):**
  - **live-vegan-conflict.** The model asked for vector recipe search,
    which returned `tool_not_configured`; it then asked the user
    instead of retrying with full-text. The run was not
    representative. **Configuration error by the AI assistant:** every
    earlier live run set `EMBEDDINGS_ENABLED=true`, but this run did
    not (the Phase 7 plan's command listed no environment, and the
    assistant missed this flag when adding the others). This also
    shows a product defect: the search tool offers vector mode when it
    is not configured, and the error does not point back to full-text.
  - **live-chicken-e2e.** The note claimed the requested "30 minutes",
    which the time/temperature guard rejected as unsupported. The
    agent then asked whether to proceed with an option whose time is
    unconfirmed. This is honest behaviour, but the scenario had no
    scripted answer for it.
  - **live-peanut-allergy.** The agent asked about the allergy and
    recorded "peanuts". After resuming, it searched "chicken
    peanut-free dinner" (0 results) and asked again instead of
    retrieving and checking recipes.
  - **live-plan-safety.** Options were reached, the user selected one,
    and the plan was correctly rejected for a missing food-safety
    reference. The session then ran out of steps (`agent_max_steps`)
    before a corrected plan. This is live evidence for P3-L-12: the
    session budget is too tight for recommend plus plan.
- **Checks seen working live:**
  - the time-claim guard;
  - the raw-poultry food-safety requirement on plans;
  - Epicure consulted;
  - the search toggle;
  - the provider-evidence citations;
  - the run ceiling and the 1-search limit.
- **Possible false positives to investigate:** the note-pairing check
  flagged "curry" (a dish name the user asked for) and "curry powder".
- Raw trajectories are in `data/phase7-live/raw/` (gitignored); the
  summary is in `data/phase7-live/live-summary-phase7.json`.

## Owner answers after the live run (2026-10-04)

Recorded by an AI assistant; not a signature.

- **The session step budget is raised from 8 to 12**
  (`SESSION_MAX_STEPS`, P3-L-12). Tool calls stay at 12. Applied in
  `config.py`, `domain/sessions.py` and `.env.example`, with the tests
  updated.
- **A live re-run of the 4 unmatched scenarios is authorized**
  (live-chicken-e2e, live-peanut-allergy, live-vegan-conflict,
  live-plan-safety):
  - embeddings on, no web searches;
  - ceiling **$0.10**, expected about $0.015;
  - run after the budget change is verified.
- Found by the assistant after the answer: the per-session input-token
  ceiling (`AGENT_INPUT_TOKEN_CEILING`, 30k) binds before 12 steps.
  The live sessions used about 3k-4.5k input tokens per turn, and
  plan-safety used 24.7k in 7 turns. So the step increase alone does
  not give the plan room. This was put back to the owner as a separate
  question.
- **Owner answer: the input-token ceiling is raised from 30k to 60k**
  (`AGENT_INPUT_TOKEN_CEILING`). The output ceiling stays 12k. The
  worst case per session is about $0.012 in model tokens. Applied in
  `config.py` and `.env.example`, with the docs updated. The loop test
  calibrated to 30k now pins 30k explicitly.

## Live re-run results (2026-10-04)

Run by an AI assistant as authorized above:
- the same command shape with `EMBEDDINGS_ENABLED=true`;
- the 4 unmatched scenarios, with the new budgets (12 steps, 60k
  input) and the fixes from the offline round;
- the working tree on top of commit `d4913b0`.

The runner's `--ceiling-usd` is cumulative for the Phase 7 pool, so
this run could spend at most $0.0708 ($0.10 − $0.0292).

- **Spend:** $0.0184, no web searches. The Phase 7 pool now stands at
  $0.0476. Milestone 3 total is about $0.2795 of $1.00.
- **Now matched (2 of 4):**
  - **live-vegan-conflict:** options, constraints honored.
  - **live-plan-safety:** options; the user selected one; the plan was
    rejected for the missing food-safety reference; the agent looked up
    safe temperatures; an accepted plan followed. This is the
    recommend-select-plan flow working end to end under the new budget.
- **Still unmatched (2 of 4), both stopped safely on the tool-call
  budget (12), with nothing unsafe offered:**
  - **live-chicken-e2e.** The note kept calling options "30-minute".
    The time guard rejected it twice, and the model never dropped the
    claim. One recipe was fetched with a malformed id. The time guard
    reads ingredient lines and directions, not titles, so a recipe
    title stating a time does not count as evidence.
  - **live-peanut-allergy.** The agent asked about the allergy first
    (improved), and the answer was recorded. Its finish then claimed a
    `dietary_constraints` key that is not a session constraint, and was
    rejected. It then re-fetched recipes it already had until the tool
    calls ran out.
- **Checkpoint C signals so far:**
  - no constraint was relaxed in any live run;
  - unsupported claims (time, pairing, missing safety reference) were
    caught by the guards before the user saw them;
  - the web citations came from provider evidence.

  The remaining failures are efficiency and recovery problems, not
  safety problems.
- Raw trajectories are in `data/phase7-live/raw-rerun/` (gitignored);
  the summary is in `data/phase7-live/live-summary-phase7-rerun.json`.
