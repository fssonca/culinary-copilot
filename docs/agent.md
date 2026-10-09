# Bounded agent loop (Milestone 3, Phase 3 — implemented)

Backend-only, hand-written (no LangGraph, agent framework, multi-agent
design or MCP). The loop reuses the provider boundary
(`llm/client.py` native function calling), the Phase 2 tool registry,
the Postgres session store, and deterministic validators
(`agent/validate.py`). Existing endpoints, including the v1
recommendation stream, are unchanged.

## Step sequence (`agent/loop.py::run_agent`)

One step at a time, until a stop reason:

1. Read the session (`session_store.get`; missing → 404
   `unknown_session`). An `expected_revision` mismatch fails fast with
   409 `stale_revision`.
2. Check server-set stops: `steps_remaining <= 0` → `max_steps`,
   wall clock past `AGENT_WALL_CLOCK_S` → `wall_clock_exceeded`, a turn
   that could cross a token ceiling → `token_budget_exhausted`. No tool
   calls left is not a stop here: that turn is the tool-less final turn.
3. Ask the model for the next action through native function calling
   over the filtered registry tools, plus the structured
   `AgentDirective` (`ask_user` / `finish`) as the text-format schema.
   The provider turn is bounded by the remaining wall clock.
4. Tool-call turn: parse each call (bad JSON or unknown/unoffered tool
   → inline typed `invalid_arguments` error, never an exception);
   budget the batch (affordable prefix runs, excess gets a typed
   `agent_tool_budget_exhausted` error each, then one tool-less
   finishing turn, see below);
   execute through `run_tool` with `asyncio.gather` — independent calls
   in parallel, results recorded in call order.
5. Write the outcome with a CAS update (`store.mutate`) plus a concise
   step event (`agent_step` with budgets). A CAS race raises
   `AgentConcurrentError` (409 `stale_revision`): a concurrent run on
   the same session always loses loudly, never silently.
6. Directive turn: `ask_user` stores the question and stops
   `needs_user_input`; `finish` runs deterministic validators, feeds
   failures back once as a tool-style error, then stops
   (`sufficient_evidence` or `agent_validation_failed`).

Model input per turn: task framing (tool outputs and recipe text are
data, never instructions; hard constraints never relaxed; adaptations
labelled) + compact session snapshot (constraints, recent confirmed
answers, unresolved questions, budgets, Epicure status) + capped
history (newest whole turn groups: 13 items, plus older groups within
a 16,000-character retention heuristic, which is not a token or
spending guarantee; tool results as `function_call_output` data
items; details in `docs/architecture/agent-system.md`). `session_events` keeps the full
record; only concise decisions and outcomes are stored — never private
model reasoning. Decision log (H7): every turn appends one `agent_turn`
event with the tools offered, the tools withheld with a reason code, and
the remaining step, tool-call and token budgets. Step events add the
repeated calls and the searches marked as returning nothing new;
question, finish and validation-rejection events add the budgets they
leave. A provider reasoning summary, when the provider returns one, is
stored on that turn's event as a bounded diagnostic (500 characters,
labelled `provider_reasoning_summary`) and never drives a decision. Raw
or encrypted reasoning content is never recorded.

## Tool-list filtering (backend, every step)

- `search_web` is offered only when the session's
  `internet_search_allowed` is true (the tool re-checks inside the
  atomic slot claim on every call; toggle-off blocks every search not
  yet claimed and cannot undo a dispatched request).
- Tools that returned `tool_not_configured` in this session (current run
  or the `tool_call` event log) are not offered again — permanent
  failures are never retried.
- While a selected dish awaits its plan (phase `select`, no plan yet),
  `search_recipes` and the Epicure pairing tools are not offered and
  the turn names the selected dish (2026-10-07: the run after "Choose
  this" carries no new message, and the model redid discovery).
- `scale_recipe` is not offered when the selected source lists no
  servings.
- A wrap-up turn withholds the tools whose calls were just repeated.

## Turn input additions (2026-10-07 live evaluation)

- Before a plan exists, a selected source with raw meat, poultry, fish
  or eggs adds a line requiring a food-safety `technique_ref`.
- After a plan exists, the snapshot carries its steps
  (`cooking_plan_steps`) so follow-up questions use them.
- When a confirmed answer names a mapped allergen, a line says the app
  checks each option's listed ingredients itself and that the allergy
  is not a `constraints_honored` key. Allergen names map to labels
  ("tree nuts", "nut allergy" and "celiac" included); a generic "nut"
  ingredient line violates tree nuts and is unverified for peanut.
- A technique or web answer given once a dish is selected keeps the
  session's phase (it used to move to `recommend`, which "plan" does
  not allow, so every follow-up question failed).
- Note and technique-answer checks skip ingredient names that are only
  negated or excluded ("contains no meat"); a technique answer may also
  name what the user's question or the selected recipe names. When a
  note fails while an option was dropped, the feedback names the drop.

## Limits (Checkpoint 0 + review, all server-side)

| Limit | Value | Source |
|---|---|---|
| Steps per session | 12 | `steps_remaining` on the session row (server-set at create, `SESSION_MAX_STEPS`) |
| Tool calls per session | 12 | `tool_calls_remaining` on the session row |
| Input tokens per session | 60,000 | `AGENT_INPUT_TOKEN_CEILING` (read before every turn) |
| Output tokens per session | 12,000 | `AGENT_OUTPUT_TOKEN_CEILING` (read before every turn) |
| Wall clock per run | 90 s | `AGENT_WALL_CLOCK_S` (read by the loop at run start) |
| Per tool call | 10 s | `TOOL_TIMEOUT_S` (registry) |

One step = one provider turn and its tool executions. A batch asking
for more calls than remain runs the affordable prefix, records a typed
error for each excess call, then gets one finishing turn without tools
instead of stopping at once (hardening step H4, 2026-10-08;
`agent/loop.py::tool_budget_stop_message`, `session_results_summary`).
Tools are then 0, so that turn is the ordinary final turn (`offered=[]`,
the same tool withholding the wrap-up uses, not a parallel path). It
passes the same top-of-loop checks as every turn: steps, wall clock,
input-token ceiling (`estimate_turn_input`) and output ceiling (at
least 500 tokens left). The loop has no separate USD spend guard; the
token ceilings and the wall clock are its spending limits. If one of
those checks fails, the run stops before any model call with
`agent_tool_budget_exhausted` (422, also when the wall clock ran out)
and the results list below. Allowances are never reset or raised.

The finishing turn happens at most once per run, and it ends the run
whatever it returns. A valid finish or question completes normally. A
rejected finish stops with the budget stop: final turns never had a
validation retry, since no step or tool remains for one. Tool calls
(none were offered, so none run) also stop with the budget stop.

The tool-budget stop message lists the useful results already in the
session, deterministically: fetched recipes (up to 5 titles with their
`dataset:source` pair, sorted by pair), options offered (up to 4, in
session order), the selected dish and the plan's step count, then
"start a new session". It reads only the event log and the session row,
never model text. Step and token stops carry the same list.

Token accounting: provider-reported usage summed from `session_events`
(step/question/finish payloads carry `input_tokens`/`output_tokens`;
no schema change). Turns that report no usage fall back to a chars/4
estimate (the repo has no token estimator), flagged
`*_tokens_estimated`. The pre-turn input estimate counts everything the
provider is sent: input items, the function definitions of the offered
tools, the `AgentDirective` response schema, and the system/instruction
text. The loop stops with `agent_token_budget_exhausted` before a turn
whose estimated input would cross the input ceiling. Output is
hard-capped per turn at `min` (configured per-turn maximum, tokens
remaining); below a documented useful minimum of 500 the turn cannot
answer, so the loop stops first. A response truncated by the cap stops
the run the same way, never as a schema failure. Measured: fixed part
~2600/turn, realistic 4-turn session ~11.3k in (real doc sizes from
`data/recipe-import/normalized.jsonl`, never the app DB), recorded live
structured outputs up to ~2k/call. Ceilings (30000 in / 12000 out)
covered ~2.6x measured and a full 8-step session. On 2026-10-04 the
owner raised steps to 12 and input to 60k: the Phase 7 live run measured
3k-4.5k input per turn, so 30k ran out after ~8 turns.

## Stop reasons, statuses and `next_action`

Budgets are per session and never reset: exhausting one says to start a
new session (`change_request`, reported as 422). The wall clock is per
run (`retry`, reported as 408). 5xx is reserved for real server or
provider faults (provider failures, DB outage, internal defects).

| Stop reason | Terminal | Status | `next_action` |
|---|---|---|---|
| `agent_max_steps` | error (start a new session) | 422 | `change_request` |
| `agent_tool_budget_exhausted` (batch excess whose finishing turn is unaffordable, rejected or calls tools; a final turn rejected with no tools left) | error (start a new session; message lists the results held) | 422 | `change_request` |
| `agent_token_budget_exhausted` | error (start a new session) | 422 | `change_request` |
| `agent_wall_clock_exceeded` | error | 408 | `retry` |
| `agent_sufficient_evidence` | final (normal completion) | — | — (terminal success, not in the error mapping) |
| `agent_needs_user_input` | final (question in `unresolved_questions`, phase `clarify`) | — | — (the question is the call to action) |
| `agent_no_progress` (3 failed steps, 3 identical calls with identical results, or repeated empty turns; the first step that only repeats earlier calls earns one wrap-up turn first, which withholds the repeated tools; a repeat-fetch pointer counts as a repeat) | error | 422 | `change_request` |
| `agent_validation_failed` (second rejection) | error | 422 | `change_request` |
| `invalid_phase_transition` (model-requested illegal move: defect, fails immediately) | error | 422 | `change_request` |

Provider errors reuse existing reasons (`provider_timeout` →
`retry`, `provider_auth` → `contact_operator`, `provider_refusal` →
`change_request`, …): retryable ones count toward the failure streak,
others fail the run at once.

## Epicure by default

Epicure is queried by default, including for specific dish requests
(owner decision 2026-09-29). Unless the model skips it with an
allowlisted reason, a session that reaches recommend has queried
Epicure (`EPICURE_SKIP_ALLOWLIST`: `simple_technique_question`,
`epicure_not_configured` — `direct_recipe_lookup` was removed). A
direct dish request may still return one recipe with Epicure
consulted, recorded with `single_option_reason: direct_dish_request`
(decoupled from skipping; `only_one_valid_candidate` is kept for a
lone survivor among several submitted). `simple_technique_question`
is refused when the request contains a pairing cue ("goes with",
"pair", "serve with", "side for" and similar, word-boundary matched)
— a narrow guard against clear misses, not semantic validation.
`epicure_not_configured` is accepted only when the configuration or a
tool outcome in the session confirms it, and is recorded as degraded
mode (`epicure_degraded` in the final and the finish event). When
Epicure is disabled or all pairing tools are not-configured, the loop
records `epicure_not_configured` itself. When the session has
successful Epicure results, a finish with options needs at least 1
model line naming a returned pairing (at least 3, or all returned
pairings if fewer, when the request has a pairing cue); each line's
ingredient must be a returned pairing name (compared lowercase,
underscores as spaces). Skipped or degraded Epicure is exempt. Each
suggestion gets one
recorded line (`used …` / `rejected …`); the outcome
(`consulted/used/rejected`) or skip reason is stored in
`epicure_outcome` / `epicure_skip_reason`. The directive may carry one
optional line per suggestion (`epicure_lines`: ingredient, used/rejected,
reason), recorded as given; suggestions the model says nothing about get
derived text prefixed `derived:` so it is never mistaken for the agent's
reason. No new `next_action` reason codes were added for this policy;
validation messages and labels (`direct_dish_request`,
`epicure_degraded`) are data, not stop reasons.

## Ask only when material, then resume

Offline, the yogurt case works end to end (`p7-ask-missing-ingredient`;
the live yogurt run went straight to options and did not exercise
this path): the agent asks when a retrieved
recipe needs an unconfirmed ingredient, the run stops
`needs_user_input` with the question in `unresolved_questions` and the
phase in `clarify`, `POST …/answers` merges the answer (CAS, question
removed), and the next run substitutes (labelled `adaptation`,
`unverified`), re-retrieves or continues.

## Outputs

- `recommend`: 1–4 sourced options (2–4 normally; a single option only
  with Epicure consulted in the session — recorded as
  `direct_dish_request` — or when several were submitted but just one
  validates — recorded as `only_one_valid_candidate` with the dropped
  ones reported). Of several submitted options every valid one is
  accepted; invalid options are never shown and every drop is reported
  (rejection feedback names `option <index> ('<title>'
  (<dataset_id>/<source_id>)): <error>`; zero valid options stays
  rejected). When any option is dropped the client sees a server note
  (`<n> option(s) were removed because they failed source checks:
  <titles>.`) instead of the model note; the model note stays in the
  finish event for review. The options final carries `note`,
  `epicure_lines`, `epicure_degraded` / `epicure_skip_reason` when set,
  and a `dropped_options` summary (`index`, `title`, `source_id`,
  first `error`). Every option passes deterministic
  validators: IDs resolve via exact `(dataset_id, source_id)` lookup,
  stated quantities match the source, adaptations carry
  `label: "adaptation"`, and every hard session constraint appears in
  `constraints_honored`. `constraints_honored` accepts only keys from
  the session's constraints (e.g. `dietary_constraints`); free-text
  claims go in `note`. Evidence rule (P3-A-01): every option's pair
  must have been returned by a successful `search_recipes` /
  `get_recipe` call in this session's events (resumed runs count;
  dataset-qualified; failures and other sessions do not count), and
  quantities need a `get_recipe` full document, not a search row.
  Minimum dietary check (P3-L-07): each option's `get_recipe`
  ingredient lines are matched against conservative vegetarian/vegan
  term lists with word boundaries (`eggplant` never flags `egg`,
  `vegetable broth` never flags `broth`). A clear violation drops the
  option with a readable reason; ambiguous terms (`broth`, `stock`,
  `bouillon`, `Worcestershire` without `vegetable`/`vegan`) keep the
  option but list the term under `constraint_check: "unverified"` in
  the client final, and the note must not claim the option is
  verified. Unknown dietary values do not invent checks
  (`constraint_check: "not_checked"` with the value).
  Minimum claim grounding (P3-L-08): Epicure-vocabulary pairings or
  companions named in the model `note` must appear in session evidence
  (returned pairings, the options' source ingredient lines, or cited
  chunks); numeric time/temperature claims must appear in a cited
  chunk or the selected recipe document. Anything else is validation
  feedback naming the unsupported terms. The client final marks the
  model note `note_source: "model"` (a server-written drop note is
  `"server"`) and `"unverified"` whenever it passes these checks only
  by containing no checkable claims. The model note is schema-bound to
  600 characters (never silently cut).
  Stored in `suggestions` (+ source refs in
  `evidence`).
- `select`: `POST …/select` stores the user's pick from the offered
  options (CAS to `selected_dish`, phase `select`). Once a dish is
  selected, a finish must be the cooking plan for it: options are
  rejected with validation feedback.
- `technique_answer`: a technique-only answer (`text` 1–1200 chars plus
  1–5 `technique_refs`), valid only with allowlisted
  `simple_technique_question` (no pairing cue) or consulted Epicure.
  The final carries the model `note` and one attribution entry per
  referenced document (`attribution_text`, `licence_url`; every chunk
  stays listed under `technique_refs`); stop reason
  `agent_sufficient_evidence`.
- `web_answer` (Phase 5, part 2, implemented offline): a cited
  discovery answer (`text` 1–1200 chars plus 1–5 `web_refs` with
  clickable `url` + `title`), labelled `evidence_class: "external"`.
  Every `web_ref.url` must equal a source URL from a successful
  `search_web` in the same session (resumed runs count). The client
  final adds one additive `label` per `web_ref` (Phase 6): the
  `classification` recorded for that exact URL in the session's
  `evidence_evaluated` events (`official_guidance` /
  `research_publication` / `culinary_source`, else `"unclassified"`),
  read through the same `session_web_sources` set the validator
  checks refs against (`agent/validate.py::web_label_for`). The
  model-facing `WebAnswer` schema and the validation are unchanged.
  Web evidence never becomes an option, a quantity, or a plan source.
  No source text actually obtained exists (provider `results` is
  image-only per docs), so time/temperature claims fail closed: a web
  answer carrying numeric claims is rejected — drop the number and
  point at the page. Procedural method is rejected too
  (`web_answer_procedural_errors`): step sequences, two or more
  imperative cooking verbs, and unit quantities. Page descriptions and
  a single serving suggestion pass. Creates a `missing_recipe` investigation
  candidate only when the request is about a recipe that local
  retrieval failed to find (zero `search_recipes` + web ok), never for
  technique or general answers.
- `plan`: `cooking_plan` (mise en place, steps, plating) from the
  selected source, with `scale_recipe` / `convert_units` results where
  asked (`scale_recipe` is not offered when the selected source lists
  no servings). The plan source must equal the selected dish **and** come from
  a `get_recipe` full document in this session's events (a search row
  is not enough). Mass and volume amounts in mise en place, steps or
  plating must equal (exactly, in any notation) an amount the source
  states for the same ingredient with the same unit (hardening step
  H2, 2026-10-07/08; `agent/plan_quantities.py`, re-exported from
  `agent/validate.py`). How an amount is tied to its ingredient:
  - by the ingredient names written in the same line: whole words,
    plural-aware, never substrings ("oil" never matches "boiled");
    the nearest mention wins, and an amount passes if any ingredient
    that mention can refer to states it, so a recipe listing butter
    twice (1/2 cup and 3 tablespoons) accepts either amount;
  - function words ("of", "the"), size words ("inch") and containers
    ("can", "package") never identify an ingredient, so "1 (28 ounce)
    can of pumpkin" is not credited to the canned tomatoes;
  - descriptors ("chopped", "ground", "fresh", "warm") identify an
    ingredient only when the line names no ingredient otherwise, so
    "1/2 cup chopped walnuts" is checked against the walnuts, not
    against "chopped pecans";
  - words run together in the source ("Buttersoftened") match by a
    prefix of at least 5 letters, only when the rest is a preparation
    word or another listed ingredient ("buttermilk" is not butter);
  - a line identical to a source ingredient line is that ingredient;
    a second amount in parentheses ("3/4 cup (178 ml)") belongs to the
    same ingredient; "chicken, 5 1/2 lb, potatoes, 1 1/2 lb" pairs
    names and amounts in order.

  An amount in a step may also match a direction the step cites via
  `step_sources`, but only when that direction states it for the same
  ingredient. An amount tied to no ingredient ("2 cups of the liquid")
  passes in a step only when a cited direction states it; mise en
  place and plating cite nothing, so there it is rejected. Rejections
  name the plan line, the amount and what the source states (or the
  candidate ingredients when the name is ambiguous).

  Limits: an ambiguous name ("1 cup oil" with vegetable oil 1 cup and
  olive oil 2 tablespoons) passes when any candidate states the
  amount; a longer name match wins ("chicken bouillon" over "chicken
  stock"); ranges ("2-3 lb") are read by one value; lines the model
  truncates mid-parenthesis may be rejected. Evidence, read-only
  (`scripts/datasets/h2_quantity_selfcheck.py`, 2026-10-08): plans
  made of each recipe's own lines pass for 16,033 of 16,033 recipes,
  "AMOUNT UNIT of NAME" lines for 16,033 of 16,033, and shortened
  model-style lines for 14,558 of 14,570 (false rejections); amounts
  swapped between two same-unit ingredients are rejected in 11,227 of
  11,229 recipes, also with a descriptor added, and the 2 misses are
  amounts the source states for both. These counts show which lines
  pass or fail on this corpus; they do not prove every wrong amount is
  caught. The `get_recipe` summary shows amounts in the source's own
  notation
  ("5 1/2", not "11/2") when both parse to the same value. Plan/cook steps
  may cite `technique_refs` (`doc_id` + `chunk_id`, max 10): each must
  resolve in the technique corpus **and** have been returned by a
  `search_techniques` call in the same session
  (`agent/validate.py::validate_technique_refs`; options carrying
  technique keys are rejected). Validated refs are stored in session
  `evidence` as `technique_refs` with `url`, `licence`,
  `licence_url`, and `attribution_text`. Technique refs are a separate
  evidence type: they support a technique claim only, never dish
  identity, quantities, or options. The `AgentDirective` schema (which
  the pre-turn input estimate measures via `model_json_schema()`)
  covers the grown `technique_refs` field inside the unchanged 30k/12k
  ceilings; technique excerpts travel in bounded tool summaries
  (600 chars/hit in history, 6500 chars per `search_techniques` output;
  raised from 300/4000 on 2026-10-06 after a live session where the
  cut hid the poultry row of the FDA temperature table).
  Minimum plan evidence (P3-L-09, tightened at Phase 7 close-out): the
  `get_recipe` summary shows the model bounded directions with
  `directions_total` / `directions_shown` / `directions_truncated`
  (up to 12 directions of up to 600 characters; a direction cut at
  600 ends with "…" and is listed in `directions_clipped`. Raised from
  6 x 200 on 2026-10-07 after a live plan lost a simmer time, two
  ingredients and the source's own thermometer check to the cut).
  Hardening step H3 (2026-10-08): `get_recipe` accepts optional
  `directions_from`/`directions_to` (0-based, `to` exclusive, at most
  12 per call) returning that slice full, without the 600-character
  cut, within the 9,000-character `get_recipe` output limit; each
  response names what it still omits. A ranged call never returns the
  duplicate pointer: it is new evidence, not a repeat of the full
  fetch (same range twice shares a digest and counts as a repeat for
  the wrap-up and stall rules). A plan for a recipe with omitted
  directions (a clipped tail or an index beyond 11) is rejected until
  every omitted index was returned full in this run (reads from an
  earlier run do not count, since that text is no longer in the
  model's history) via such calls, or the plan names each unread
  index in an adaptation (for example "directions 12, 13 not read")
  and is `model_adaptation`. Because a rejection uses the run's one
  validation retry, the turn input states the requirement up front
  once a dish is selected and no plan exists ("Plan requirement:
  directions [12, 13] ...", with the exact call, covering scattered
  unread indices in one call where they fit in 12), and drops it once
  they are read. A recipe fetched only to compare options needs no
  extra calls: its summary says to read the rest only if it is
  planned. The rejection names the uncovered indices and the next
  `directions_from`/`to` call. Corpus read-only 2026-10-08: 605 of
  16,033 recipes need the path (184 with a clipped direction, 421
  with more than 12); extra calls needed with 12 per call: 1 for 589,
  2 for 13, 3 for 2, 4 for 1 (worst 52 directions, 5 calls total with
  the first fetch, within the 12-call ordinary and 40-call demo
  budgets).
  Attribution folds accents ("jalapeño" matches "jalapeno") and
  ignores bare citation tags such as "[Source direction 2]".
  `steps_source` is `"source"` only when every plan step cites a
  stored direction index (`step_sources`) whose direction contains all
  the step's content words, **and** every stored direction is cited by
  at least one step. Otherwise it is `"model_adaptation"` (shown in the
  client final), and at least one plan adaptation must state the steps
  are not from the source. A plan claiming the source has no
  directions is rejected when the stored record has them. Hardening
  step H3 part 2 (2026-10-08): when `steps_source` is
  `model_adaptation`, the model note, every adaptation description and
  every plan text field (mise en place, steps, plating) must not claim
  the steps follow, match or reproduce the source
  (`agent/validate.py::plan_fidelity_errors`: deterministic patterns
  for follow, match, reproduce/replicate, identical to, same as,
  verbatim, word for word, faithful, true to, exact copy, exactly as
  in, no changes, sticks to, taken/copied from, and "source ... with
  no changes"). A negation counts only within the three words before
  the verb, in the same clause, so "does not follow the source" passes
  but "Without changing anything, this follows the original" is a
  claim; for "verbatim" and "word for word" a negation anywhere earlier
  in the clause counts. A claim about numbered steps ("Steps 1-3
  follow the source; step 4 is mine") is allowed. Limits: keyword
  patterns, not meaning, so other paraphrases pass; copied source
  directions saying "Follow the recipe through step N" are rejected in
  an adaptation step (3 lines in 2 of 16,033 recipes, read-only check
  2026-10-08). A `source` plan is not affected. The rejection names the field and the matched claim so the
  one validation retry can remove it. The plan client final carries the
  model note (`note`, `note_source: "model"`) with the validated
  `plan.steps_source`; the UI shows that label next to the note as well
  as on the steps, never parsing the note text for the label. Source ingredients with raw meat, poultry, fish
  or eggs (not "cooked") need at least one `technique_ref` to a chunk
  from a food-safety manifest doc (`tech-fda-safe-32`,
  `tech-fsis-temp-34`, `tech-fda-kitchen-33`, `tech-fsis-leftover-36`);
  the feedback tells the model to `search_techniques` for safe
  internal temperatures. Since 2026-10-07 the turn input also states
  this requirement once a dish is selected (and names the returned
  food-safety chunks once found), so the model no longer learns it
  from a rejected plan.

## Endpoints (`api/agent.py`)

| Endpoint | Behavior |
|---|---|
| `POST /api/v1/sessions/{id}/answers` `{revision, question_id, answer}` | merge + drop question (CAS); 404 `unknown_question`, 409 `stale_revision` |
| `POST /api/v1/sessions/{id}/select` `{revision, dataset_id, source_id}` | pick from offered options (CAS); 422 `unknown_option`, 409 `stale_revision` |
| `POST /api/v1/sessions/{id}/agent/stream` `{expected_revision?}` | SSE: `stage` events (concise outcomes, never recipe text or reasoning), then exactly one `final` or one `error` (which carries `next_action`); mid-run CAS race → single 409 `stale_revision` error |

## New settings (read, not dead)

- `AGENT_WALL_CLOCK_S` (default 90): read by `agent/loop.py` at run
  start. Steps/tool calls are session-row budgets, not settings.
- `AGENT_INPUT_TOKEN_CEILING` (default 60000 since 2026-10-04) and
  `AGENT_OUTPUT_TOKEN_CEILING` (default 12000): read before every turn;
  usage summed from `session_events`; per-turn output hard-capped at
  `min(6500, remaining)` with a 500-token useful minimum. All three
  documented in `.env.example`.

## Review packet

`evals/phase3_agent/`: `generate.py` (offline, scripted fakes,
disposable `culinary_check_packet` DB) renders `trajectories.json` +
`REVIEW.md` (10 trajectories in plain language: normal recommend +
select + plan, yogurt, direct+select+plan, constraint conflict, empty
retrieval, tool failure, budget, wall clock, Epicure skip,
no-progress; every option/plan shows title, IDs, source facts vs
labelled adaptations, plus budgets and token totals after each run).
`LIVE_PLAN.md` was the Phase 3 live plan (gpt-6-luna, repo-recorded
pricing, measured ceilings, $0.15 bound, 8 live cases).

The Phase 7 offline harness is `evals/phase7_agent/` (v8, 43 cases;
`docs/agent-scoreboard.md`). The Phase 7 live evaluation (11 sessions)
and its owner review are in `evals/phase7_agent/CHECKPOINT_C.md` and
`docs/phase7-owner-decisions.md`.
