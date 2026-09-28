# Phase 3 review packet: 10 offline trajectories

Generated 2026-09-28 21:33 UTC by `evals/phase3_agent/generate.py` from actual loop outputs (scripted fake provider + fake tools, disposable `culinary_check_packet` database). No model calls, no network, no paid calls. Live evaluation is a separate, unrun plan (`LIVE_PLAN.md`).

Conventions: `stop_reason` is the stable loop reason; tool outcomes are `ok`/`error_type`/`reason`; Epicure lines record each suggestion's use or rejection in one line.

Token sizes are full pre-turn estimates (input items, offered tool definitions, directive schema), chars/4 — the repo has no token estimator; per-run and per-session totals come from the same accounting the loop enforces (provider-reported when present, estimated otherwise). Budgets after each run show steps and tool calls remaining plus session token totals.

## Normal full flow (recommend, select, plan) (`normal-full-flow`)

User request: I want a chicken dinner for tonight, about 30 minutes.

Run 1: stopped with `agent_sufficient_evidence`.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1): from the source: 500 g chicken.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620, 2871]; this run used 5491 in / 195 out.
Budgets after: 6 steps, 10 tool calls, 5491 input tokens, 195 output tokens used in session.

Run 2: stopped with `agent_sufficient_evidence`.
Plan from (odunola/foodie, curry-1): 2 mise en place items, 3 steps, plating: over rice in shallow bowls. from the source: 500 g chicken.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2651, 2817]; this run used 5468 in / 154 out.
Budgets after: 4 steps, 9 tool calls, 10959 input tokens, 349 output tokens used in session.

Tools called: search_recipes (ok); find_balanced_pairings (ok); get_recipe (ok).
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
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620, 2918]; this run used 5538 in / 119 out.
Budgets after: 6 steps, 10 tool calls, 5538 input tokens, 119 output tokens used in session.

Run 2: stopped with `agent_sufficient_evidence`.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1): from the source: 500 g chicken adaptation (labelled adaptation): unverified substitution: coconut milk for yogurt (Epicure candidate, no dietary claim).
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2633, 2769]; this run used 5402 in / 196 out.
Budgets after: 4 steps, 9 tool calls, 10940 input tokens, 315 output tokens used in session.

Tools called: search_recipes (ok); get_recipe (ok); find_substitutions (ok).
Question asked: `q-yogurt`.
Epicure: derived: used coconut milk: appears in the final options.
Epicure: derived: rejected sour cream: not used in the final options.
Epicure outcome: consulted:2 used:1 rejected:1.
Confirmed answers kept: [{'answer': 'no', 'question_id': 'q-yogurt'}].
Final phase: `recommend`.

## Direct recipe request (skips select) (`direct-recipe`)

User request: Give me the red lentil soup recipe.

Run 1: stopped with `agent_sufficient_evidence`.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620, 2770]; this run used 5390 in / 130 out.
Budgets after: 6 steps, 11 tool calls, 5390 input tokens, 130 output tokens used in session.

Run 2: stopped with `agent_sufficient_evidence`.
Plan from (odunola/foodie, lentil-2): 2 mise en place items, 3 steps, plating: in deep bowls with lemon.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2648]; this run used 2648 in / 87 out.
Budgets after: 5 steps, 11 tool calls, 8038 input tokens, 217 output tokens used in session.

Tools called: search_recipes (ok).
Epicure skipped: direct_recipe_lookup.
Epicure skip reason: direct_recipe_lookup.
Selected dish: {'title': 'Red Lentil Soup', 'source_id': 'lentil-2', 'dataset_id': 'odunola/foodie'}.
Cooking plan plating: in deep bowls with lemon.
Final phase: `plan`.

## Hard-constraint conflict (rejected, then honored) (`hard-constraint-conflict`)

User request: Vegetarian dinner, something hearty.

Run: stopped with `agent_sufficient_evidence`.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2630, 2767]; this run used 5397 in / 227 out.
Budgets after: 6 steps, 12 tool calls, 5397 input tokens, 227 output tokens used in session.

Epicure skipped: direct_recipe_lookup.
Epicure skip reason: direct_recipe_lookup.
Final phase: `recommend`.

## Empty retrieval (asks user to rephrase) (`empty-retrieval`)

User request: Dragonfruit soufflé glacé.

Run: stopped with `agent_needs_user_input`.
Asked: I found no recipes for that. What dish should I look for?
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620, 2721]; this run used 5341 in / 90 out.
Budgets after: 6 steps, 11 tool calls, 5341 input tokens, 90 output tokens used in session.

Tools called: search_recipes (ok).
Question asked: `q-rephrase`.
Final phase: `clarify`.

## Tool failure (Epicure not configured, no retry) (`tool-failure`)

User request: Chicken pairings for a roast.

Run: stopped with `agent_sufficient_evidence`.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1): from the source: 500 g chicken.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620, 2740]; this run used 5360 in / 195 out.
Budgets after: 6 steps, 10 tool calls, 5360 input tokens, 195 output tokens used in session.

Tools called: find_balanced_pairings (tool_not_configured); search_recipes (ok).
Epicure skipped: epicure_not_configured.
Epicure skip reason: epicure_not_configured.
Final phase: `recommend`.

## Budget exhaustion (batch exceeds tool calls) (`budget-exhaustion`)

User request: Compare five chicken dishes.

Run: stopped with `agent_tool_budget_exhausted`.
Error 422: batch exceeded the tool-call budget (1 excess not run); start a new session (next_action: change_request).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620]; this run used 2620 in / 85 out.
Budgets after: 7 steps, 0 tool calls, 2620 input tokens, 85 output tokens used in session.

Tools called: search_recipes (ok); search_recipes (ok).
Final phase: `discover`.

## Wall-clock stop (`wall-clock-stop`)

User request: A slow search day.

Run: stopped with `agent_wall_clock_exceeded`.
Error 408: wall clock exceeded; start a new run to continue (next_action: retry).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620]; this run used 2620 in / 33 out.
Budgets after: 7 steps, 11 tool calls, 2620 input tokens, 33 output tokens used in session.

Tools called: search_recipes (ok).
Final phase: `discover`.

## Epicure skip (simple technique question) (`epicure-skip`)

User request: How do I boil an egg, and what soup goes with it?

Run: stopped with `agent_sufficient_evidence`.
Option 'Red Lentil Soup' (odunola/foodie, lentil-2): from the source: 200 g red lentils.
Option 'Creamy Chicken Curry' (odunola/foodie, curry-1): from the source: 500 g chicken.
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620]; this run used 2620 in / 141 out.
Budgets after: 7 steps, 12 tool calls, 2620 input tokens, 141 output tokens used in session.

Epicure skipped: simple_technique_question.
Epicure skip reason: simple_technique_question.
Final phase: `recommend`.

## No progress (identical calls, no new information) (`no-progress`)

User request: Chicken please.

Run: stopped with `agent_no_progress`.
Error 422: repeated identical search_recipes calls without new information (next_action: change_request).
Turn input sizes (full pre-turn estimate, chars/4 tokens): [2620, 2768, 2899]; this run used 5388 in / 68 out.
Budgets after: 6 steps, 10 tool calls, 5388 input tokens, 68 output tokens used in session.

Tools called: search_recipes (ok); search_recipes (ok); search_recipes (ok).
Final phase: `discover`.
