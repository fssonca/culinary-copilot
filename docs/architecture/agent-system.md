# The agent system

Part of the [current architecture](README.md). Code snapshot: `main` at
`a4422c6`, reviewed 2026-10-11 (Milestone 3 closed after hardening
H0-H8; the demo fixes D0 merged; the Milestone 4 design in ADR 0003 is
accepted but not implemented). Claims cite the code, test or record
that shows them.

This page explains how the cooking agent works, end to end. It is meant
to be read in one sitting. The detailed contracts are in
[agent](../agent.md), [tools](../tools.md), [sessions](../sessions.md)
and [ADR 0002](../adr/0002-agent-loop.md).

## 1. What it does

A user describes a meal ("Give me the creamy chicken curry recipe",
"Vegan dinner, something hearty"). The agent:

1. searches a corpus of stored recipes;
2. consults Epicure, a local ingredient-pairing model;
3. asks one question when the answer would change the result;
4. offers 2–3 sourced options;
5. after the user picks one, produces a cooking plan with food-safety
   references.

With the user's permission, it can also search the web and answer with
cited pages.

The model chooses what to do next. Code decides what is allowed to
reach the user.

## 2. Design principles

| Principle | How it shows up |
|---|---|
| The model proposes, code disposes | Every final answer passes deterministic validators before the user sees it. Rejected answers are sent back once as feedback, then the run stops. |
| Tool outputs are data, never instructions | The task framing says so. Recipe text, technique chunks and web pages travel as `function_call_output` data. Nothing in them can widen permissions or budgets. |
| Deny by default | Web search is off per session and enforced in the backend, not just hidden in the UI. Paid and network tools are labelled by cost class. |
| Bounded by construction | Budgets are per session and server-set: by default 12 steps, 12 tool calls, 60k input and 12k output tokens, plus 90 s of wall clock per run (`config.py:143-144`, `:306`, `:320-321`). `make demo` uses 40, 40, 300k, 60k and 240 s (`Makefile:13-23`). Each stop has a stable reason and a `next_action`. |
| Fail typed, not loud | Tool failures are typed results (`invalid_arguments`, `timeout`, `unavailable`, `tool_not_configured`) that the model can act on. Permanent failures are not offered again. |
| State lives in the database | Sessions use compare-and-swap writes. Events are append-only, enforced by a Postgres trigger. A restart loses nothing. |
| Unknown stays unknown | Unknown quantities and unverified dietary or allergen status are shown as unknown, never guessed. |

## 3. Components

```mermaid
flowchart TB
    UI["/ui page (static ES modules, CSP, textContent only)"] -->|"run / stream (SSE), answers, select, permission"| API["FastAPI agent endpoints"]
    API --> Loop["Agent loop (agent/loop.py)"]
    Loop -->|"read + CAS write"| Store[("Postgres: sessions + append-only session_events")]
    Loop -->|"native function calling"| LLM["Model provider boundary (llm/client.py)"]
    Loop -->|"offered tools, filtered per step"| Registry["Typed tool registry (tools/registry.py)"]
    Registry --> RecipeTools["search_recipes, get_recipe"]
    Registry --> EpicureTools["find_flavor/conventional/balanced_pairings, find_substitutions"]
    Registry --> MeasureTools["scale_recipe, convert_units"]
    Registry --> TechTools["search_techniques (food-safety + technique corpus)"]
    Registry --> WebTool["search_web (permission + slot claim)"]
    RecipeTools --> PG[("Postgres: recipes, pgvector embeddings, techniques")]
    TechTools --> PG
    EpicureTools --> Epicure["Local Epicure vectors (CPU)"]
    WebTool --> Hosted["Hosted web search sub-request"]
    Registry -->|"tool_call events"| Store
    Loop --> Validators["Deterministic validators (agent/validate.py)"]
    Validators --> Loop
```

| Part | File | Role |
|---|---|---|
| Loop | `agent/loop.py` | Step control, context assembly, budgets, stop reasons, finish handling |
| Validators | `agent/validate.py`, `agent/plan_quantities.py` | Evidence, quantity, constraint, allergen, citation and plan-attribution checks; amounts in plan text tied to source ingredients |
| Registry | `tools/registry.py` | Pydantic argument models (`extra="forbid"`), server-set timeouts, cost class, typed errors, event logging |
| Tools | `tools/*.py` | 10 tools across recipes, Epicure, measurement, techniques and web |
| Sessions | `domain/sessions.py`, migration `005` | Phase table, budgets, CAS revisions, append-only events |
| API | `api/agent.py` | Run, SSE stream, answers, select, permission |
| UI | `web/` | Message box, streamed stages, cards per answer shape, question answering, search toggle |

The loop is hand-written, without a framework: 5,150 lines for the
loop, 2,047 for the validators and 1,025 for plan-quantity attribution
(`wc -l` at `a4422c6`; about 3,700 and 1,400 on 2026-10-05). A
framework would replace the step loop, call fan-out and history
bookkeeping. It would not replace the session schema, the registry,
permission enforcement, the validators or the spend ledger. Those are
product and safety boundaries, not orchestration (ADR 0002).

## 4. One run, step by step

```mermaid
sequenceDiagram
    participant U as User (/ui)
    participant A as Agent API
    participant L as Loop
    participant S as Session store
    participant M as Model
    participant T as Tools
    participant V as Validators
    U->>A: message (expected_revision)
    A->>L: run_agent(session_id)
    loop until a stop reason
        L->>S: read session; check budgets and wall clock
        L->>L: build turn input (framing + snapshot + evidence digest + capped history)
        L->>M: offered tools + directive schema
        alt tool calls
            M-->>L: function calls
            L->>T: run in parallel, record in call order
            T-->>S: tool_call events (outcome, latency, cost class, identities)
            L->>S: CAS update + agent_step event
        else ask_user
            M-->>L: question
            L->>S: store question; phase clarify
            L-->>U: stop agent_needs_user_input
        else finish
            M-->>L: options / plan / technique answer / web answer
            L->>V: deterministic checks
            alt accepted
                L->>S: suggestions or plan + finish event
                L-->>U: stop agent_sufficient_evidence
            else rejected
                L->>M: feedback once (names the failed check)
            end
        end
    end
```

Stops: `agent_sufficient_evidence` and `agent_needs_user_input` are
normal endings. The error stops are:

- `agent_max_steps`, `agent_tool_budget_exhausted` and
  `agent_token_budget_exhausted`: 422, start a new session;
- `agent_wall_clock_exceeded`: 408, retry;
- `agent_no_progress` and `agent_validation_failed`: 422.

Before `agent_no_progress`, the loop tries once to recover: when a
step only repeats earlier calls with identical results, the next
turn withholds the repeated tools and asks the model to use what it
has: fetch candidates it has not fetched yet, or answer (a wrap-up
turn). Until 2026-10-07 the wrap-up offered no tools at all; with
one recipe fetched, the model could then only ask the user a
question. A rejected wrap-up answer is retried with the same tools
withheld. A third identical call no longer stops the run at once
(after H8 attempt 1, where both sessions stopped that way with usable
evidence): the run gets one tool-less finishing turn to finish from
what it has or ask one question; a tool call, a rejected answer or a
limit on that turn stops with `agent_no_progress` as before, at most
once per run (`agent/loop.py:2629-2637`, `tests/test_stall_recovery.py`).
A repeat `get_recipe` that returns the "already fetched" pointer
counts as a repeat for both rules, even though the pointer differs
from the full output it points at. An identical repeated search is
marked in its output as returning nothing new, and a full-text
search with fewer than 3 results suggests broadening the query.

When a batch asks for more tool calls than remain, the affordable
prefix runs and the model gets one finishing turn without tools
instead of stopping at once (H4, 2026-10-08). It is the ordinary final
turn (`offered=[]`) and passes the same step, wall-clock and token
checks as every turn; the token ceilings and the wall clock are the
spending limits. It happens at most once per run and ends the run: a
valid finish or question completes; a rejected finish (no retry on a
final turn) or a tool call stops. When a check fails or the turn is
rejected, the run stops with `agent_tool_budget_exhausted` (reason
unchanged) and a deterministic message listing fetched recipes,
options, the selected dish and the plan. Step and token stops list the
same results. Allowances are never reset or raised.

The UI renders one outcome card per reason.

## 5. Context management

Every turn is rebuilt from durable state. Nothing depends on a long
in-memory transcript.

- **Task framing:** fixed rules, covering data versus instructions,
  never relaxing constraints, labelling adaptations, the answer shapes
  and the Epicure policy.
- **Session snapshot:** constraints, confirmed answers, open questions,
  remaining budgets and Epicure status; once a plan exists, its first
  12 steps (`agent/loop.py:1445-1450`).
- **Phase lines:** after an allergy answer and before a dish is
  selected, a line says the app checks each option's listed ingredients
  itself (`agent/loop.py:1469-1482`). In the plan run, the selected
  recipe's `get_recipe` output is part of the input and a re-fetch
  returns a pointer (`agent/loop.py:1484-1500`; added after H8 attempt
  3, where every plan run fetched the recipe three times). After a plan,
  a line asks for a `technique_answer` grounded in the plan and returned
  chunks, not a re-issued plan; a request for a different dish is
  treated as a new request that finishes with options
  (`agent/loop.py:1501-1516`).
- **Evidence digest:** rebuilt from `session_events` every turn. It
  lists searches, fetched recipes, web sources and technique hits, so
  the model knows what it already has after history is trimmed or a
  new run starts.
- **Capped history:** the newest whole turn groups up to 13 items,
  plus older whole groups while the history stays within 16,000
  characters (at most 31 items), so small outputs such as pairings
  are not pushed out by one large recipe step. The character limit is
  a retention heuristic, not a token or spending guarantee: the
  input-token ceiling and the run budgets limit spending. A function
  call is never separated from its output. Each tool output
  is summarized to IDs and the facts needed, bounded at 4,000
  characters (6,500 for technique search, whose hits carry
  attribution and 600-character excerpts; 9,000 for `get_recipe`)
  with structural truncation that stays valid JSON.
- **Repeat fetches:** a recipe already visible in the run is not
  re-sent. The model gets a named pointer to its earlier output, never
  an empty recipe it could misread as "no directions".
- **Recipe evidence:** ingredients plus bounded directions (up to 12,
  each up to 600 characters), with `directions_total`,
  `directions_shown`, `directions_truncated` and `directions_clipped`
  (directions cut at 600 characters, which end with "…"). "The source
  has no directions" and "directions were cut here" are therefore
  different facts. The bound was 6 x 200 until 2026-10-07: 64% of
  corpus recipes had a longer direction, and the cut was silent. Since
  2026-10-08 (H3) the model reads omitted text with `get_recipe`
  `directions_from`/`to` (0-based, at most 12 full directions per
  call, each response within its output limit and naming what it
  still omits). On the current corpus (15,875 recipes after the H5
  cleanup) 65 recipes have a clipped direction and 486 need the ranged
  path (counted 2026-10-11 with `doc_directions` and
  `omitted_direction_indices` from `agent/validate.py`); before the
  cleanup the counts were 184 and 605 of 16,033.
- **Visibility-aware de-duplication:** a repeated `get_recipe` returns
  a short pointer only when the full document is still in the model's
  visible history. Otherwise it returns the full document. Ranged
  reads never return the pointer: the same pair with a direction range
  is new evidence (same range twice shares a digest and counts as a
  repeat for wrap-up and stall).
- **Token accounting:** before each turn, the loop estimates everything
  it will send (items, tool definitions, directive schema) and stops
  before crossing the ceiling. The final turn is sent with no tools, so
  the model must answer or ask.

## 6. Memory and state

- **Short-term:** the capped history inside one run.
- **Session memory:** the `sessions` row, which holds:
  - the phase, constraints and answers;
  - the selected dish and remaining budgets;
  - the search permission and a revision number.

  Every write is compare-and-swap, so a concurrent run loses loudly
  with 409 and never silently overwrites.
- **Event log:** `session_events` is append-only (a trigger rejects
  UPDATE and DELETE). It records user messages, steps, tool calls,
  questions, answers, selections, validation rejections and finishes.
  Review tools, the evidence digest and the validators all read the
  same log.
- **Phases as data:** `discover`, `clarify`, `research`, `recommend`,
  `select`, `plan`, `cook` and `plate`, with an explicit transition
  table. An illegal model-requested move fails with
  `invalid_phase_transition`.
- **Durability:** sessions survive an API restart. The offline case
  `p7-restart-survives` asks a question, restarts, answers, resumes and
  finishes.

There is no long-term user memory across sessions by design. Each
session starts from the request.

## 7. Human in the loop

| Approval point | Mechanism |
|---|---|
| Missing information | `ask_user` stops the run. `POST …/answers` merges the answer (CAS) and the next run resumes with it. |
| Choosing a dish | `POST …/select` records the pick and moves the session to `select` (`agent/loop.py:1951-1956`); the next finish must be a plan for exactly that dish. After a finished plan, a request for a different dish may finish with new options: the session moves to `recommend`, the selection and plan are cleared, and the earlier plan is kept in the finish event as `superseded_plan` (D0; `domain/sessions.py:57-69`, `tests/test_agent_loop.py::test_new_dish_after_plan_returns_options`). Before the plan, options for a selected dish are still rejected. |
| Dietary constraint | The page's Diet selector sets `dietary_constraints` (vegetarian or vegan) when the session is created. It is a hard constraint: every option is checked against its ingredient list and is never relaxed. |
| Internet access | The operator switch `WEB_SEARCH_ENABLED` (off by default) connects a search provider at all. Then the per-session toggle, off by default. `search_web` is offered only when it is on, and re-checks permission inside an atomic slot claim (at most 3 per session). |
| Budget exhaustion | The stop says what to do next (start a new session or retry) and lists the useful results already held (fetched recipes, options, selected dish, plan). The UI shows a matching card (reason unchanged; the message carries the list). |

## 8. Guardrails: what the validators enforce

The validators run on every finish, with no model call:

- **Identity:** each option resolves by exact `(dataset_id, source_id)`
  and was returned in this session.
- **Quantities:** any stated quantity must match the fetched document
  exactly (rational comparison).
- **Constraints:**
  - hard dietary constraints are checked against ingredient lines;
  - ambiguous terms stay `unverified`, for example broth, or Parmesan
    for vegetarians (usually made with animal rennet);
  - allergen checks use three tiers (violated, unverified,
    no_listed_terms_found), never "safe", with a disclaimer;
  - the checks read listed ingredients only and cannot rule out
    cross-contact;
  - a confirmed answer counts as allergy evidence when the session
    mentions an allergy or avoidance, when its question asked about
    one, or when the answer itself states one. A plain dish choice
    ("Creamy mushroom pasta") is not read as a wheat allergy.
- **Claims:** pairing terms in the note must appear in session evidence;
  the user's request and confirmed answers count as evidence, so a note
  warning about the allergen the user named is not an unsupported
  pairing (`confirmed_answer_texts`, `agent/loop.py:2420`; after H8
  attempt 2). Times and temperatures must appear in the cited chunk or
  recipe.
- **Plans:**
  - the source must be the selected dish, fetched in full;
  - mass and volume amounts in the plan text must equal an amount the
    source states for the same ingredient with the same unit
    (hardening step H2, `agent/plan_quantities.py`; amounts reach the
    model in the source's own notation, and the UI shows 11/2 as a
    mixed number). Amounts are tied to ingredients by the names in the
    same line: whole words, ignoring function words, size words,
    containers and, when a real name is present, descriptors. An
    amount passes if any ingredient its name can refer to states it. A
    step may also rely on a cited direction that states the amount for
    the same ingredient; amounts tied to no ingredient pass only
    through a cited direction. In a list line ("Gather A, B, C") an
    amount attaches to the item it precedes (`_forward_list_attachment`,
    `agent/plan_quantities.py:684`; after H8 attempt 3), and a
    hyphenated size before an equipment noun ("12-cup muffin tin") is
    not an amount (`_equipment_size`, `agent/plan_quantities.py:42-66`;
    after H8 attempt 4). Rules, limits and corpus counts are in
    `docs/agent.md` (self-check script
    `scripts/datasets/h2_quantity_selfcheck.py`). The Milestone 4
    readiness sweep still found 402 Foodie recipes whose own lines fail
    list attribution, a classified check defect
    (`docs/m4/r1-corpus-readiness.md`, section 2);
  - steps are labelled `source` only when each step cites a stored
    direction that supports it and every direction is covered;
    otherwise `model_adaptation`. When the source has directions,
    the app writes the adaptation note itself, naming the steps and
    the words that differ; an ingredient-only source still needs the
    model's own admission. Matching folds accents and ignores bare
    citation tags in step text. Since H3 (2026-10-08) a plan for a
    recipe with omitted directions (clipped tail or index beyond 11)
    also needs every omitted index read full in this run via ranged
    `get_recipe`, or an adaptation naming each unread index plus
    `model_adaptation` (the turn input states this, with the exact
    call, before the plan is drafted); attribution already checks full
    stored directions, so ranged reads count;
  - raw meat, poultry, fish or eggs require a food-safety technique
    reference (technique search treats chicken, turkey, duck and goose
    as also matching "poultry", the word the FDA guidance uses). Once
    a dish is selected, the turn input states this requirement up
    front instead of leaving it to a rejected plan. Only the two FDA
    documents are in the corpus (both FSIS pages were blocked at fetch
    time), so they are the only accepted references. The raw-protein
    list includes cured meats such as pepperoni and salami
    (`agent/validate.py:1068`), which made a demo pizza plan cite food
    safety; no source in the corpus classifies them, so the list stays
    strict (`docs/milestone-4-plan.md`, D0 item 2);
  - a plan that claims the source has no directions is rejected when
    it does;
  - since H3 part 2 (2026-10-08) a `model_adaptation` plan whose note,
    adaptation or plan text claims the steps follow, match or
    reproduce the source is rejected (deterministic patterns with
    negation handling; a `source` plan is unaffected). The plan final
    carries the model note with the validated `steps_source`, and the
    UI shows that label next to the note as well as on the steps. A
    clause-initial instruction to follow a doneness cue of the source
    ("follow the source's doneness cues") is advice, not a claim (D0,
    after it ended a demo plan;
    `tests/test_agent_validate.py::test_plan_fidelity_advice_to_follow_a_source_cue_is_not_a_claim`).
- **Web answers:**
  - every URL must come from the provider's own citation evidence for
    this session;
  - numbers fail closed;
  - procedural cooking method is rejected (discovery only: pointers
    plus page descriptions).
- **Epicure:** queried by default. A skip needs an allowlisted reason,
  and a consulted finish must name returned pairings.

## 9. Security and privacy

- **Permissions:**
  - web search is permission-gated per session in the backend;
  - the session ID is bound by the server, never passed as a tool
    argument (argument models forbid extra fields).
- **Prompt injection:**
  - retrieved content is framed as data;
  - validators check facts against stored evidence, not the model's
    account of them.
- **Output handling:**
  - the UI renders with `textContent` only;
  - links pass through `safeLink` (http and https only,
    `noopener noreferrer`);
  - a strict Content-Security-Policy is set by a pure ASGI middleware.
- **Data minimization:**
  - tool events store an argument digest, not the raw arguments, by
    default;
  - when raw trajectories are recorded for review, emails, phone
    numbers, street addresses and named people are scrubbed;
  - no private model reasoning is stored;
  - search events have a documented purge procedure.
- **Secrets:** kept in `.env`, out of Git and logs. Default tests need
  no keys.

## 10. Observability and cost

- **Event log:** every tool call records outcome, error type, reason,
  latency, cost class, mode and returned identities. Every step
  records remaining budgets and token usage. Every turn records one
  `agent_turn` decision event (H7): the tools offered and withheld
  with the reason each (`search_permission_off`, `epicure_disabled`,
  `tool_not_configured`, `no_servings`, `select_phase_plan_only`,
  `repeat_wrap_up`, `final_turn_no_tools`, `mode_not_configured`),
  the repeated results it saw (`repeated_tools`, `repeat_noted` on
  the step event), validation failures with the budgets they leave
  behind, and the remaining step/tool/token budgets. A provider
  reasoning summary, when returned, is stored once on that turn's
  outcome event as a bounded diagnostic labelled
  `provider_reasoning_summary` (500 chars) and never used for a
  decision; raw or encrypted reasoning content is never recorded. All
  payloads stay bounded.
- **Full trajectories (opt-in):** with `AGENT_RECORD_TRAJECTORY=true`
  (on in `make demo`), the log also records:
  - tool arguments;
  - what each tool returned to the model;
  - every model directive, including rejected answers;
  - each run's result.

  All text is scrubbed of personal details and bounded.
  `scripts/sessions/export_session.py` exports a session as JSON plus a
  readable timeline. It reads inside a read-only transaction.
- **Streaming:** SSE stage events show progress (concise outcomes,
  never recipe text or reasoning) and end in exactly one `final` or
  `error`.
- **Live runs:** go through a runner with a spend ledger:
  - each model turn reserves its worst-case cost before it is sent;
  - each search reserves its estimate;
  - a turn that cannot be afforded stops the scenario;
  - pools and ceilings are cumulative, and every limit is enforced
    inside the run, not just at preflight.
- **Spend:** the live runner's four ledgers record $0.4453375 across
  every ledgered live run to 2026-10-09, of which $0.0320108 are
  reservations kept unresolved after ambiguous calls. Not in any
  ledger: the 2026-10-07 five-session evaluation (about $0.081 by token
  count), demo preparation on 2026-10-09 (about $0.02) and the owner's
  demo sessions (unknown). Provider billing has not been compared
  (`evals/h8_live/OWNER_REVIEW.md`, "Spend reconciliation").

## 11. Evaluation

| Layer | What it proves | Size |
|---|---|---|
| Unit and integration tests | Contracts, validators, tools, CAS, UI headers (`make check`, disposable databases) | 1,332 passed, 8 skipped (`main`, 2026-10-11); CI runs the same suite against a Postgres service |
| Offline scenario harness (`evals/phase7_agent/run.py`) | The real loop, tools and validators against scripted model turns, including adversarial ones (skipped Epicure, invented URL, bad arguments, illegal transition) | 61/61 cases (v14), 4/4 adversarial caught (`evals/phase7_agent/results.json`) |
| Scorer mutation tests | Each scoring check actually fails when the behaviour breaks | `tests/test_phase7_scorer.py` |
| Bounded live runs, Phase 7 | 7 scenarios, then 4 re-runs after fixes, all trajectories saved | First run 3/7 expected stops; re-runs 2/4 (`evals/phase7_agent/CHECKPOINT_C.md`) |
| Bounded live runs, H8 | Two workflows per attempt, demo limits, fresh scenarios each attempt, fixes between attempts | 8 sessions over 4 attempts, 2 complete (one per workflow); a later smoke re-run of attempt 4's scenarios completed 2/2 (`evals/h8_live/RESULTS.md`) |
| Human checkpoints | The owner reads live trajectories beside their sources, and decides | C (2026-10-05), D (2026-10-08, `docs/phase7-owner-decisions.md`), E (2026-10-09, `docs/milestone-4-plan.md`); the H8 transcript verdict is open (`evals/h8_live/OWNER_REVIEW.md`) |

How a live finding becomes a regression test: each live failure is
reproduced as a scripted offline case, fixed, and pinned before any
re-run. Examples:

- the time-claim loop;
- redundant fetches;
- the plan that ignored its source directions;
- the web answer that taught a method.

The harness version and case hash are recorded, so a changed case
means a new version.

Offline results measure the system, not the model's judgement. Live
recovery after an answer with allergy-checked options was shown once,
in H8 attempt 3 (question, recorded answer, checked options, plan,
follow-up), at demo limits and with recorded limitations: the question
came only after 16 steps (`evals/h8_live/OWNER_REVIEW.md`). One
completion per workflow shows the workflows can complete; it does not
establish reliability, which needs repeated runs of one frozen build.

## 12. Known limits and next steps

- **Single agent.** No multi-agent design, framework or tracing
  platform is planned: Milestone 3 put them out of scope
  (`docs/milestone-3-hardening-plan.md:424`), and Milestone 4 is about
  answers built on stored recipes (ADR 0003). CI runs `make check`
  against a Postgres service and a Docker smoke test.
- **Keyword-based checks.** Dietary, allergen, claim and web-method
  checks are deterministic and conservative, not semantic. A
  single-verb instruction can pass the web guard.
- **Few live runs.** Ledgered live evidence is the 11 Phase 7 sessions
  (one web search), the 8 H8 sessions and a 2-session smoke re-run, plus
  an unledgered five-session evaluation on 2026-10-07: diagnostic, not a
  benchmark.
- **Limits.** Every H8 session ran at the `make demo` limits (40 steps,
  40 calls, 300k tokens). Behavior at the default limits (12, 12, 60k)
  has not been re-measured since two Phase 7 flows ended on the tool
  budget after a resume.
- **Known false positive.** Cured meats such as pepperoni need a
  food-safety citation (open, `docs/milestone-4-plan.md`, D0 item 2).
  The other demo defects were fixed in D0; they have offline cases
  (harness v14) but no live run yet.
