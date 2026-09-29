# Phase 3 review packet: 13 offline trajectories

Generated 2026-09-29 22:19 UTC by `evals/phase3_agent/generate.py` from actual loop outputs (scripted fake provider + fake tools, disposable `culinary_check_packet` database). No model calls, no network, no paid calls. Live evaluation is a separate, unrun plan (`LIVE_PLAN.md`).

Regenerated 2026-09-29 under P3-A-01 (options need same-session retrieval evidence; quantities and plans need get_recipe) and P3-A-02 (Epicure queried by default including direct requests; `direct_recipe_lookup` removed; pairing-cue guard; degraded mode). Three scenarios show the new rejections; the empty-retrieval question now offers a concrete choice without claiming an alternative was found (owner Q1 note).

Conventions: `stop_reason` is the stable loop reason; tool outcomes are `ok`/`error_type`/`reason`; Epicure lines record each suggestion's use or rejection in one line.

Token sizes are full pre-turn estimates (input items, offered tool definitions, directive schema), chars/4 — the repo has no token estimator; per-run and per-session totals come from the same accounting the loop enforces (provider-reported when present, estimated otherwise). Budgets after each run show steps and tool calls remaining plus session token totals.

## Normal full flow (recommend, select, plan) (`normal-full-flow`)

User request: I want a chicken dinner for tonight, about 30 minutes.

Run 1: stopped with `agent_sufficient_evidence`.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1): from the source: 500 g chicken.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3423]; this run used 6296 in / 269 out.
Budgets after: 6 steps, 8 tool calls, 6296 input tokens, 269 output tokens used in session.

Run 2: stopped with `agent_sufficient_evidence`.
Plan from (odunola/foodie, curry-1): 2 mise en place items, 3 steps, plating: over rice in shallow bowls. from the source: 500 g chicken.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2903, 3070]; this run used 5973 in / 154 out.
Budgets after: 4 steps, 7 tool calls, 12269 input tokens, 423 output tokens used in session.

Tools called: search_recipes (ok); get_recipe (ok); get_recipe (ok); find_balanced_pairings (ok); get_recipe (ok).
Epicure: derived: rejected pork: not used in the final options.
Epicure: derived: rejected beef: not used in the final options.
Epicure outcome: consulted:2 used:0 rejected:2.
Selected dish: {'title': 'Creamy Chicken Curry', 'source_id': 'curry-1', 'dataset_id': 'odunola/foodie'}.
Cooking plan plating: over rice in shallow bowls.
Final phase: `plan`.

## Yogurt ask-and-resume (`yogurt-ask-resume`)

User request: Something with chicken; I might have yogurt.

Run 1: stopped with `agent_needs_user_input`.
Asked: Do you have plain yogurt?
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3170]; this run used 6043 in / 119 out.
Budgets after: 6 steps, 10 tool calls, 6043 input tokens, 119 output tokens used in session.

Run 2: stopped with `agent_sufficient_evidence`.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1): from the source: 500 g chicken adaptation (labelled adaptation): unverified substitution: coconut milk for yogurt (Epicure candidate, no dietary claim).
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2885, 3172]; this run used 6057 in / 233 out.
Budgets after: 4 steps, 8 tool calls, 12100 input tokens, 352 output tokens used in session.

Tools called: search_recipes (ok); get_recipe (ok); find_substitutions (ok); get_recipe (ok).
Question asked: `q-yogurt`.
Epicure: derived: used coconut milk: appears in the final options.
Epicure: derived: rejected sour cream: not used in the final options.
Epicure outcome: consulted:2 used:1 rejected:1.
Confirmed answers kept: [{'answer': 'no', 'question_id': 'q-yogurt'}].
Final phase: `recommend`.

## Direct recipe request (Epicure consulted, one option) (`direct-recipe`)

User request: Give me the red lentil soup recipe.

Run 1: stopped with `agent_sufficient_evidence`.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Single-option reason recorded: direct_dish_request.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3277]; this run used 6150 in / 186 out.
Budgets after: 6 steps, 9 tool calls, 6150 input tokens, 186 output tokens used in session.

Run 2: stopped with `agent_sufficient_evidence`.
Plan from (odunola/foodie, lentil-2): 2 mise en place items, 3 steps, plating: in deep bowls with lemon.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2902]; this run used 2902 in / 87 out.
Budgets after: 5 steps, 9 tool calls, 9052 input tokens, 273 output tokens used in session.

Tools called: search_recipes (ok); find_balanced_pairings (ok); get_recipe (ok).
Epicure: derived: rejected coconut milk: not used in the final options.
Epicure: derived: rejected sour cream: not used in the final options.
Epicure outcome: consulted:2 used:0 rejected:2.
Selected dish: {'title': 'Red Lentil Soup', 'source_id': 'lentil-2', 'dataset_id': 'odunola/foodie'}.
Cooking plan plating: in deep bowls with lemon.
Final phase: `plan`.

## Hard-constraint conflict (rejected, then honored) (`hard-constraint-conflict`)

User request: Vegetarian dinner, something hearty.

Run: stopped with `agent_sufficient_evidence`.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Single-option reason recorded: direct_dish_request.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2883, 3438, 3485]; this run used 9806 in / 357 out.
Budgets after: 5 steps, 8 tool calls, 9806 input tokens, 357 output tokens used in session.

Tools called: search_recipes (ok); get_recipe (ok); get_recipe (ok); find_balanced_pairings (ok).
Epicure: derived: rejected coconut milk: not used in the final options.
Epicure: derived: rejected sour cream: not used in the final options.
Epicure outcome: consulted:2 used:0 rejected:2.
Final phase: `recommend`.

## Empty retrieval (asks user to rephrase) (`empty-retrieval`)

User request: Dragonfruit soufflé glacé.

Run: stopped with `agent_needs_user_input`.
Asked: I found no recipes for dragonfruit soufflé glacé. Want me to look for a lemon dessert instead?
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 2974]; this run used 5847 in / 114 out.
Budgets after: 6 steps, 11 tool calls, 5847 input tokens, 114 output tokens used in session.

Tools called: search_recipes (ok).
Question asked: `q-rephrase`.
Final phase: `clarify`.

## Tool failure (Epicure not configured, no retry) (`tool-failure`)

User request: Chicken pairings for a roast.

Run: stopped with `agent_sufficient_evidence`.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1): from the source: 500 g chicken.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3292]; this run used 6165 in / 268 out.
Budgets after: 6 steps, 8 tool calls, 6165 input tokens, 268 output tokens used in session.

Tools called: find_balanced_pairings (tool_not_configured); search_recipes (ok); get_recipe (ok); get_recipe (ok).
Epicure skipped: epicure_not_configured.
Epicure degraded mode recorded.
Epicure skip reason: epicure_not_configured.
Final phase: `recommend`.

## Budget exhaustion (batch exceeds tool calls) (`budget-exhaustion`)

User request: Compare five chicken dishes.

Run: stopped with `agent_tool_budget_exhausted`.
Error 422: batch exceeded the tool-call budget (1 excess not run); start a new session (next_action: change_request).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873]; this run used 2873 in / 85 out.
Budgets after: 7 steps, 0 tool calls, 2873 input tokens, 85 output tokens used in session.

Tools called: search_recipes (ok); search_recipes (ok).
Final phase: `discover`.

## Wall-clock stop (`wall-clock-stop`)

User request: A slow search day.

Run: stopped with `agent_wall_clock_exceeded`.
Error 408: wall clock exceeded; start a new run to continue (next_action: retry).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873]; this run used 2873 in / 33 out.
Budgets after: 7 steps, 11 tool calls, 2873 input tokens, 33 output tokens used in session.

Tools called: search_recipes (ok).
Final phase: `discover`.

## Epicure skip (simple technique question) (`epicure-skip`)

User request: How do I boil an egg?

Run: stopped with `agent_sufficient_evidence`.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2):.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1):.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3020]; this run used 5893 in / 146 out.
Budgets after: 6 steps, 11 tool calls, 5893 input tokens, 146 output tokens used in session.

Tools called: search_recipes (ok).
Epicure skipped: simple_technique_question.
Epicure skip reason: simple_technique_question.
Final phase: `recommend`.

## Skip rejected (direct_recipe_lookup removed) (`skip-rejected-direct-lookup`)

User request: Vegetarian dinner, something hearty.

Run: stopped with `agent_validation_failed`.
Error 422: validation rejected: epicure skip reason 'direct_recipe_lookup' not allowlisted (allowlist: ['epicure_not_configured', 'simple_technique_question']); single option needs Epicure consulted in this session (submit 2+ options otherwise) (next_action: change_request).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2883, 3184, 3315]; this run used 9382 in / 272 out.
Budgets after: 5 steps, 10 tool calls, 9382 input tokens, 272 output tokens used in session.

Tools called: search_recipes (ok); get_recipe (ok).
Final phase: `discover`.

## Skip rejected (pairing cue in technique question) (`skip-rejected-pairing-cue`)

User request: How do I boil an egg, and what soup goes with it?

Run: stopped with `agent_validation_failed`.
Error 422: validation rejected: simple_technique_question refused: the request asks for a pairing ('goes with'); query Epicure instead (next_action: change_request).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3021, 3097]; this run used 8991 in / 260 out.
Budgets after: 5 steps, 11 tool calls, 8991 input tokens, 260 output tokens used in session.

Tools called: search_recipes (ok).
Final phase: `discover`.

## Options rejected (valid ID never retrieved) (`evidence-rejected-unretrieved`)

User request: I want a chicken dinner for tonight, about 30 minutes.

Run: stopped with `agent_validation_failed`.
Error 422: validation rejected: Epicure not queried and no allowlisted skip reason (allowlist: ['epicure_not_configured', 'simple_technique_question']); option 0: option 0: (odunola/foodie, curry-1) was not retrieved in this session (next_action: change_request).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3015]; this run used 5888 in / 164 out.
Budgets after: 6 steps, 12 tool calls, 5888 input tokens, 164 output tokens used in session.

Final phase: `discover`.

## No progress (identical calls, no new information) (`no-progress`)

User request: Chicken please.

Run: stopped with `agent_no_progress`.
Error 422: repeated identical search_recipes calls without new information (next_action: change_request).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2873, 3021, 3152]; this run used 5894 in / 68 out.
Budgets after: 6 steps, 10 tool calls, 5894 input tokens, 68 output tokens used in session.

Tools called: search_recipes (ok); search_recipes (ok); search_recipes (ok).
Final phase: `discover`.
