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
   `tool_calls_remaining <= 0` → `tool_budget_exhausted`, wall clock
   past `AGENT_WALL_CLOCK_S` → `wall_clock_exceeded`.
3. Ask the model for the next action through native function calling
   over the filtered registry tools, plus the structured
   `AgentDirective` (`ask_user` / `finish`) as the text-format schema.
   The provider turn is bounded by the remaining wall clock.
4. Tool-call turn: parse each call (bad JSON or unknown/unoffered tool
   → inline typed `invalid_arguments` error, never an exception);
   budget the batch (affordable prefix runs, excess gets a typed
   `agent_tool_budget_exhausted` error each and the run stops);
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
history (first item plus newest; tool results as
`function_call_output` data items). `session_events` keeps the full
record; only concise decisions and outcomes are stored — never private
model reasoning.

## Tool-list filtering (backend, every step)

- `search_web` is offered only when the session's
  `internet_search_allowed` is true (the tool re-checks inside the
  atomic slot claim on every call; toggle-off blocks every search not
  yet claimed and cannot undo a dispatched request).
- Tools that returned `tool_not_configured` in this session (current run
  or the `tool_call` event log) are not offered again — permanent
  failures are never retried.

## Limits (Checkpoint 0 + review, all server-side)

| Limit | Value | Source |
|---|---|---|
| Steps per session | 8 | `steps_remaining` on the session row (server-set at create) |
| Tool calls per session | 12 | `tool_calls_remaining` on the session row |
| Input tokens per session | 20,000 | `AGENT_INPUT_TOKEN_CEILING` (read before every turn) |
| Output tokens per session | 5,000 | `AGENT_OUTPUT_TOKEN_CEILING` (read before every turn) |
| Wall clock per run | 90 s | `AGENT_WALL_CLOCK_S` (read by the loop at run start) |
| Per tool call | 10 s | `TOOL_TIMEOUT_S` (registry) |

One step = one provider turn and its tool executions. A batch asking
for more calls than remain runs the affordable prefix, records a typed
error for each excess call, and stops with `tool_budget_exhausted`.

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
cover ~2.6x measured and a full 8-step session; overshoot is impossible
since 8 steps bound totals by construction.

## Stop reasons, statuses and `next_action`

Budgets are per session and never reset: exhausting one says to start a
new session (`change_request`, reported as 422). The wall clock is per
run (`retry`, reported as 408). 5xx is reserved for real server or
provider faults (provider failures, DB outage, internal defects).

| Stop reason | Terminal | Status | `next_action` |
|---|---|---|---|
| `agent_max_steps` | error (start a new session) | 422 | `change_request` |
| `agent_tool_budget_exhausted` | error (start a new session) | 422 | `change_request` |
| `agent_token_budget_exhausted` | error (start a new session) | 422 | `change_request` |
| `agent_wall_clock_exceeded` | error | 408 | `retry` |
| `agent_sufficient_evidence` | final (normal completion) | — | — (terminal success, not in the error mapping) |
| `agent_needs_user_input` | final (question in `unresolved_questions`, phase `clarify`) | — | — (the question is the call to action) |
| `agent_no_progress` (3 failed steps, 3 identical calls with identical results, or repeated empty turns) | error | 422 | `change_request` |
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

The yogurt case works end to end: the agent asks when a retrieved
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
  point at the page. Creates a `missing_recipe` investigation
  candidate only when the request is about a recipe that local
  retrieval failed to find (zero `search_recipes` + web ok), never for
  technique or general answers.
- `plan`: `cooking_plan` (mise en place, steps, plating) from the
  selected source, with `scale_recipe` / `convert_units` results where
  asked. The plan source must equal the selected dish **and** come from
  a `get_recipe` full document in this session's events (a search row
  is not enough). Plan/cook steps
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
  (300 chars/hit in history, 4000 chars/turn).
  Minimum plan evidence (P3-L-09): a source without directions sets
  `steps_source: "model_adaptation"` on the plan (shown in the client
  final), and at least one plan adaptation must state the steps are
  not from the source. Source ingredients with raw meat, poultry, fish
  or eggs (not "cooked") need at least one `technique_ref` to a chunk
  from a food-safety manifest doc (`tech-fda-safe-32`,
  `tech-fsis-temp-34`, `tech-fda-kitchen-33`, `tech-fsis-leftover-36`);
  the feedback tells the model to `search_techniques` for safe
  internal temperatures.

## Endpoints (`api/agent.py`)

| Endpoint | Behavior |
|---|---|
| `POST /api/v1/sessions/{id}/answers` `{revision, question_id, answer}` | merge + drop question (CAS); 404 `unknown_question`, 409 `stale_revision` |
| `POST /api/v1/sessions/{id}/select` `{revision, dataset_id, source_id}` | pick from offered options (CAS); 422 `unknown_option`, 409 `stale_revision` |
| `POST /api/v1/sessions/{id}/agent/stream` `{expected_revision?}` | SSE: `stage` events (concise outcomes, never recipe text or reasoning), then exactly one `final` or one `error` (which carries `next_action`); mid-run CAS race → single 409 `stale_revision` error |

## New settings (read, not dead)

- `AGENT_WALL_CLOCK_S` (default 90): read by `agent/loop.py` at run
  start. Steps/tool calls are session-row budgets, not settings.
- `AGENT_INPUT_TOKEN_CEILING` (default 30000) and
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
`LIVE_PLAN.md` is the prepared, unrun live plan (gpt-6-luna,
repo-recorded pricing, measured ceilings, $0.15 bound, 8 live cases).
