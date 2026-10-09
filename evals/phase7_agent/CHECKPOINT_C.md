# Checkpoint C packet (Phase 7, Milestone 3)

Offline close-out plus the owner-run live results (first run $0.0292,
re-run $0.0184; no further paid runs authorized). Model text below is
paraphrased, never quoted: notes, questions and answers are described
by what they did, not reproduced. Recipe ids, stored titles, URLs and
counts are evidence, not model output. Budgets are now 12 steps and
60k input tokens per session (owner decision, checkpoint 0); the
trajectories below ran under those budgets where noted.

## 1. First-run sessions (raw-rerun/ holds the re-runs)

### 1a. live-chicken-e2e — `data/phase7-live/raw/live-chicken-e2e.json`

- Scenario and synthetic request: weeknight chicken dinner in about
  30 minutes.
- Tools: find_balanced_pairings 1, search_recipes 3, get_recipe 1
  (all ok); 4 steps; about 14.5k input / 166 output tokens.
- Guards fired: time/temperature guard once, rejecting a note that
  repeated the requested 30 minutes (no selected recipe states it).
  Epicure was consulted.
- Final shape: clarifying question offering a choice among found
  options (v1 had no scripted accept, so the flow stopped here).
- Recipes cited: none offered (question stop).
- constraint_check: none (question stop).
- Stop and cost: `agent_needs_user_input`; pool $0.0292 of $0.15.

### 1b. live-yogurt-ask — `data/phase7-live/raw/live-yogurt-ask.json`

- Scenario and synthetic request: chicken dish, possibly with yogurt
  on hand.
- Tools: find_balanced_pairings 1, search_recipes 1, get_recipe 3
  (all ok); 2 steps; about 6.7k input / 175 output tokens.
- Guards fired: pairing guard once, rejecting a note term present
  only in a fetched-but-unselected recipe; the accepted final
  dropped it. The agent asked no question here: it went straight to
  options (no missing-ingredient ask occurred in this run).
- Final shape: two options.
- Recipes cited: odunola/foodie foodie-013603 (Tandoori Chicken),
  odunola/foodie foodie-013240 (Greek Yogurt Chicken Salad).
- constraint_check: none (no hard session keys).
- Stop and cost: `agent_sufficient_evidence`; pool $0.0292 of $0.15.

### 1c. live-peanut-allergy — `data/phase7-live/raw/live-peanut-allergy.json`

- Scenario and synthetic request: chicken dinner for a friend with an
  unnamed food allergy.
- Tools: search_recipes 4, find_balanced_pairings 1, get_recipe 1
  (all ok); 6 steps; about 20.9k input / 181 output tokens.
- Guards fired: none (the failure was model judgment, not a guard).
  The allergy was asked about before any safe-option search and the
  answer was recorded; after resuming, the model searched a
  constraint-worded query (0 results) and asked again, claiming
  ingredients were unretrieved although the first run had fetched one
  (the evidence digest listed the fetch).
- Final shape: clarifying question (second ask).
- Recipes cited: none offered.
- constraint_check: none (question stops).
- Stop and cost: `agent_needs_user_input`; pool $0.0292 of $0.15.

### 1d. live-vegan-conflict — `data/phase7-live/raw/live-vegan-conflict.json`

- Scenario and synthetic request: hearty vegan dinner.
- Tools: search_recipes 1 (error: `tool_not_configured`), plus
  pairing/technique calls; 4 steps; about 15.5k input / 167 output
  tokens.
- Guards fired: the vector request failed closed (configuration
  error: embeddings off with 16033 recipe embeddings in the DB, so
  vector was offered but unusable); the model asked the user instead
  of retrying with full-text.
- Final shape: clarifying question; the run was not representative.
- Recipes cited: none offered.
- constraint_check: none (question stop).
- Stop and cost: `agent_needs_user_input`; pool $0.0292 of $0.15.

### 1e. live-search-once — `data/phase7-live/raw/live-search-once.json`

- Scenario and synthetic request: a dish missing from the local
  corpus, search permission on.
- Tools: search_recipes 1 (empty), search_web 1, pairing 1 (all ok);
  3 steps; about 10.1k input / 86 output tokens.
- Guards fired: none; the discovery path worked as designed.
- Final shape: web answer with three refs, all from provider
  citation evidence: japan-food.jetro.go.jp (Taste of Japan),
  pbs.org (food recipes), justonecookbook.com (authentic recipe page).
- constraint_check: none.
- Stop and cost: `agent_sufficient_evidence`; pool $0.0292 of $0.15
  (the run's single web search).

### 1f. live-search-toggle — `data/phase7-live/raw/live-search-toggle.json`

- Scenario and synthetic request: a lentil-soup recipe, search
  permission off from the start.
- Tools: search_recipes 1, get_recipe 3, pairing 1 (all ok, no web
  offered); 5 steps; about 18.0k input / 158 output tokens.
- Guards fired: none needed; the gate held (no search dispatch).
- Final shape: two options.
- Recipes cited: odunola/foodie foodie-007123 (lentil soup),
  odunola/foodie foodie-007126 (lentil soup variant).
- constraint_check: none.
- Stop and cost: `agent_sufficient_evidence`; pool $0.0292 of $0.15.

### 1g. live-plan-safety — `data/phase7-live/raw/live-plan-safety.json`

- Scenario and synthetic request: the creamy chicken curry recipe.
- Tools: pairing 1, search_recipes 1, get_recipe 3; 4 steps; about
  13.7k input / 168 output tokens.
- Guards fired: pairing guard once (dish-name term from the request;
  now a support source) and the raw-poultry plan rule once (missing
  food-safety reference, correctly rejected).
- Final shape: two options (odunola/foodie foodie-013643 and
  foodie-013614, both chicken curries); the plan run then stopped on
  the 8-step budget of the time.
- constraint_check: none (no hard session keys).
- Stop and cost: `agent_max_steps`; pool $0.0292 of $0.15.

## 2. Re-run sessions (new budgets: 12 steps, 60k input)

### 2a. live-chicken-e2e — `data/phase7-live/raw-rerun/live-chicken-e2e.json`

- Same request (30 minutes kept for the guard behaviour); v2
  scenario added a scripted accept, which was recorded against the
  choose-among-three question.
- Tools: pairings 2, search_recipes 3, get_recipe 6 ok + 1 error
  (dataset-prefixed id); 7 steps; about 31.3k input / 415 output
  tokens.
- Guards fired: time guard twice (unsupported 30-minute claims; the
  model never dropped the claim), invalid-arguments once (malformed
  id, now with the expected-format message).
- Final shape: none (budget stop).
- Recipes cited: none offered.
- constraint_check: none.
- Stop and cost: `agent_tool_budget_exhausted` (12 calls); pool
  $0.0184 of the $0.0708 re-run allowance.

### 2b. live-peanut-allergy — `data/phase7-live/raw-rerun/live-peanut-allergy.json`

- Same request; the allergy was asked about first and the answer
  recorded.
- Tools: search_recipes 4, pairings 2, get_recipe 6 (incl. two
  repeat fetches of already-returned pairs); 8 steps; about 32.7k
  input / 449 output tokens.
- Guards fired: `constraints_honored` misuse once (claimed a key the
  empty session lacks; now answered with leave-it-empty guidance).
- Final shape: none (budget stop).
- Recipes cited: none offered.
- constraint_check: none (no finished final).
- Stop and cost: `agent_tool_budget_exhausted` (12 calls); pool
  $0.0184 of the $0.0708 re-run allowance.

### 2c. live-vegan-conflict — `data/phase7-live/raw-rerun/live-vegan-conflict.json`

- Same request, embeddings on.
- Tools: search_recipes 2, get_recipe 3, pairings 2 (all ok);
  4 steps; about 16.8k input / 226 output tokens.
- Guards fired: none; vector search ran, three vegan options offered.
- Final shape: three options, all `checked` for vegan.
- Recipes cited: odunola/foodie foodie-016650, foodie-012953,
  foodie-007108 (chickpea/lentil curries and soup).
- constraint_check: all three `checked` / vegan.
- Stop and cost: `agent_sufficient_evidence`; pool $0.0184 of the
  $0.0708 re-run allowance.

### 2d. live-plan-safety — `data/phase7-live/raw-rerun/live-plan-safety.json`

- Same request.
- Tools: search_recipes 1, get_recipe 3, pairings 1,
  search_techniques 5 (all ok); 8 steps; about 34.8k input / 327
  output tokens.
- Guards fired: raw-poultry plan rule once (missing safety ref);
  the model then looked up safe temperatures and finished an
  accepted plan citing a food-safety chunk.
- Final shape: options, then a plan for odunola/foodie foodie-013643
  with technique ref tech-fda-safe-32 chunk 6.
- constraint_check: none.
- Stop and cost: `agent_sufficient_evidence`; pool $0.0184 of the
  $0.0708 re-run allowance.

## 3. Checkpoint C questions (answers for the owner's decision)

### Were any claims unsupported?

Answer: yes, unsupported content reached the user. The accepted
re-run plan for the cashew chicken curry described the recipe record
as holding ingredients but no directions, changed the cooking
method, and called that an adaptation — yet the stored record holds
6 directions (including a refrigerated marinade and cashews blended
with cold water), and the server labelled the steps "source" merely
because directions exist. The accepted search-once web answer taught
a cooking method (batter, cabbage, optional proteins, pan-frying,
flipping) beyond the discovery-only boundary of pointers plus page
descriptions. Both are fixed offline, not live-verified (plan
attribution with cited direction indices; procedural web guard).
Other flagged claims never reached the user: repeated 30-minute
notes, the unoffered-recipe ingredient, both missing safety
references. Resolved since: all 24 quantity entries in the two
yogurt options were re-verified read-only against the stored
recipes (13 + 11 lines, all matching — the quantity caveat is
closed), and the 165°F poultry instruction is supported by the
cited FDA chunk ("Poultry (ground, parts, whole, and stuffing) |
165 °F"). Recommendation: fixed offline; live confirmation carried
to Milestone 4. No further paid run is needed to establish the
fixes; the v8 offline grounding cases
(time/pairing/quantity/ref/attribution/web checks, all passing) are
the standing proof.

### Were any constraints relaxed?

Answer: no explicit constraint relaxation was observed in the
reviewed live runs. The vegan checks ran (three re-run options
`checked` for vegan); the peanut sessions asked, recorded the
answer and resumed, but stopped on budget stops without options, so
allergy-aware recommendation after resuming is unproven. Allergen
checks cover listed-ingredient evidence only and cannot establish
absence of cross-contact. Recommendation: fixed offline where a
fix applies (honored-misuse message, refetch rule); live
confirmation carried to Milestone 4. No further paid run is needed
to establish the fixes.

### Were the search citations real and relevant?

Real: all three answer URLs are recorded in provider evidence (call
ids through the slot-claimed dispatch). Relevance: the owner
checked the JETRO and Just One Cookbook pages as relevant; the PBS
page returned 403 to the owner, so it is not fully reviewed.
Against: only one live search exists, and the answer content
exceeded the discovery-only boundary (item 2, fixed offline).
Recommendation: accept provisional on the owner's page read; keep
the per-session cap for any further runs.

## 4. Open findings carried to Milestone 4

Recovery fixes below are marked offline only; not live-evaluated.
Scripted offline cases prove specified behaviour, not that a live
model will choose the intended recovery.

- Vector-when-unconfigured: fixed offline, not live-verified
  (regression: `p7-vector-unconfigured`).
- Note time-claim loop: fixed offline, not live-verified
  (regressions: `p7-time-repeat`, `p7-time-dropped`,
  `p7-time-stored-title`, `p7-time-model-title`).
- Honored-misuse: fixed offline, not live-verified (regression:
  `p7-honored-misuse`).
- Redundant fetches: fixed offline, not live-verified (regressions:
  `p7-refetch-duplicate`, `p7-plan-refetch-full`,
  `p7-double-fetch-pointer`).
- Plan attribution (direction claims, false-absence rejection,
  full-direction coverage): fixed offline, not live-verified
  (regressions: `p7-plan-false-absence`, `p7-plan-faithful-source`,
  `p7-plan-partial-coverage`, `p7-plan-changed-method`).
- Web discovery-only boundary: fixed offline, not live-verified
  (regression: `p7-web-answer-method`).
- Tool-call budget binding end-to-end runs (12): open, defaults
  unchanged (no regression case pins a budget value; the harness
  budget-exhaustion cases pin the typed stops).
- Technique-mode comparison: open (no runner support; needs a
  paired-run flow plus query embeddings).
- Live confirmation of plan attribution: a live plan whose steps cite
  stored-direction indices and whose label the owner can check
  against the cited directions.
- Live confirmation of the web discovery-only guard: a live web
  answer accepted with page descriptions only, or a procedural
  attempt rejected with the guard's message.

Milestone 4 carry-overs, each with an explicit acceptance criterion:

- Live ask-and-resume to checked options: a live run that asks,
  records, resumes and delivers options with per-option
  constraint_check entries.
- Live allergy-aware recommendation after resuming: a live resumed
  final whose allergen answers are recorded and whose options carry
  non-violated allergen checks.
- The technique-mode comparison: paired full-text/vector runs over
  the same queries with per-mode evidence, priced separately.
- Whether a bounded post-answer or post-selection allowance under a
  cumulative session ceiling is needed: evaluate only after the
  above fixes, against fresh live trajectories — not from the
  offline harness.

## 5. Completion criteria (Milestone 3 execution plan)

- Epicure queried by default, justified skips recorded: met.
  Offline: `p7-epicure-default`, `p7-epicure-skip-justified`,
  `p7-epicure-degraded`, `p7-vector-unconfigured`. Live: pairing
  calls ok in all 11 sessions (verified per trajectory).
- Ask about a newly discovered missing ingredient and resume: met
  offline; partly met live (ask, record and resume shown; resumed
  options not delivered live). Offline: `p7-ask-missing-ingredient`,
  `p7-ask-allergy-peanuts`, `p7-restart-survives`,
  `p7-resume-uses-evidence`, `p7-refetch-duplicate`,
  `p7-plan-refetch-full`, `p7-double-fetch-pointer`. Live: the peanut
  runs asked, recorded the answer and resumed (but ended on budget
  stops with no checked options delivered); the yogurt run went
  straight to options with no question asked.
- Recommendations reference retrieved recipes, adaptations
  distinguished: met offline; live attribution failed once (the
  cashew plan, above) and is fixed offline. Offline: select/plan
  cases, source-reference correctness 43/43 (v8 harness). Live:
  cited ids/titles above; plans cite the selected recipe.
- Disabled search not invocable through the backend: met. Offline:
  `p7-search-off-refused` (loop gate plus direct backend probe).
  Live: toggle run dispatched no search.
- Searches produce references, logs, gap records: met. Offline:
  `p7-search-on-cited`, `p7-search-limit-reached`. Live: one
  dispatched search, three evidenced refs, ledger + raw logs.
- Iteration terminates within limits without silent relaxation:
  partly met. Offline: budget cases stop with typed reasons; no
  relaxation anywhere. Live terminal outcomes across the 11
  sessions: 5 sufficient-evidence, 3 questions, 3 budget stops — six
  ended in a question or budget stop (the first plan attempt had
  offered options before its later budget stop). The budget-shape
  question is open.
- Session state survives a restart: met offline
  (`p7-restart-survives`: fresh store/app objects on the same
  disposable DB, resume to options). Not exercised live (no runner
  support for mid-run restarts).
