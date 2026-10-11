# ADR 0002: Bounded agent loop

Status: **implemented (Milestone 3, Phases 1-7). Budgets raised on
2026-10-04 to 12 steps and 60k input tokens, after the live run.
Phase 7 offline harness and live evaluation completed; checkpoint C
answered on 2026-10-05.**
Reviewed 2026-10-11 against `main` (`e27a6fa`, then `a4422c6` after
the D0 merge): the decision stands; changes made during hardening
(H0-H8, 2026-10-07 to 2026-10-08) and D0 are listed under "Later
changes" with their code and tests.
This records the design, its limits and what a framework would replace.

## Context

Milestone 3 needs a cooking assistant that works through a session
(discover to plate) using typed tools, asks only when material, keeps
hard constraints, cites evidence and stops within budgets. The loop is
hand-written (no LangGraph, agent framework, multi-agent design or
MCP), reusing the provider boundary (`llm/client.py` native function
calling), the Phase 2 tool registry, the Postgres session store
(migration 005) and deterministic validators (`agent/validate.py`).
Existing endpoints, including v1 recommendation streaming, are
unchanged.

## Decision

One step at a time in `agent/loop.py::run_agent`, until a stable stop
reason:

1. Read the session (`session_store.get`; missing 404
   `unknown_session`). An `expected_revision` mismatch fails fast 409
   `stale_revision`.
2. Check server-set stops: `steps_remaining <= 0` max_steps,
   `tool_calls_remaining <= 0` tool_budget_exhausted, wall clock past
   `AGENT_WALL_CLOCK_S` wall_clock_exceeded.
3. Ask the model via native function calling over the filtered
   registry tools, plus `AgentDirective` (`ask_user` / `finish`) as the
   text-format schema. The turn is bounded by the remaining wall clock.
4. Tool-call turn: parse each call (bad JSON or unknown/unoffered tool
   inline typed `invalid_arguments`); budget the batch (affordable
   prefix runs, excess gets typed `agent_tool_budget_exhausted` and
   the run stops); execute through `run_tool` with `asyncio.gather`
   (independent calls in parallel, results in call order).
5. Write the outcome with a CAS update plus a concise step event. A
   CAS race raises `AgentConcurrentError` (409): a concurrent run
   always loses loudly, never silently.
6. Directive turn: `ask_user` stores the question and stops
   `needs_user_input`; `finish` runs deterministic validators, feeds
   failures back once, then stops (`sufficient_evidence` or
   `agent_validation_failed`).

## Limits and budgets

Server-side, per session, never reset (Checkpoint 0 + review):

- Steps 12 (8 until 2026-10-04), tool calls 12 (session row,
  server-set at create).
- Input tokens 60,000 (30,000 until 2026-10-04), output tokens 12,000
  (`AGENT_INPUT_TOKEN_CEILING`,
  `AGENT_OUTPUT_TOKEN_CEILING`, read before every turn; usage summed
  from `session_events`; pre-turn input estimate counts items, offered
  tool defs, directive schema and framing; per-turn output capped at
  `min(6500, remaining)` with a 500-token useful minimum).
- Wall clock 90 s per run (`AGENT_WALL_CLOCK_S`); per tool call 10 s
  (`TOOL_TIMEOUT_S`), except `search_web` 30 s
  (`SEARCH_WEB_TIMEOUT_S`, owner decision 2026-10-03).

One step is one provider turn and its tool executions. Budgets say to
start a new session (`change_request`, 422); the wall clock says to
retry the run (`retry`, 408). 5xx is for real server/provider faults.
Phase 7 offline cooperative flows use 4 steps and 4-6 tool calls. The
live run measured 3k-4.5k input tokens per turn, and a successful plan
flow used 34.8k, so the owner raised the ceilings (see
`docs/agent-scoreboard.md` and `docs/phase7-owner-decisions.md`).
The defaults are still 12, 12, 60,000, 12,000 and 90 s
(`config.py:143-144`, `:306`, `:320-321`). `make demo` sets 40 steps,
40 tool calls, 300,000 / 60,000 tokens and 240 s (`Makefile:13-23`);
the H8 live checks ran at those limits, so the defaults have no live
measurement since Phase 7.

## Tool boundaries and permission enforcement

- Registry: name, pydantic schemas (`extra="forbid"`), server-set
  timeout, idempotency flag, cost class (free / paid / network).
  Failures are typed error results, never exceptions into the loop.
- `search_web` is offered only when `internet_search_allowed` is true,
  and re-checks inside the atomic slot claim on every call.
  Toggle-off blocks every search not yet claimed. The session id is
  server-bound per run; the args model has no session field.
  Per-session slots: 3 in code, 1-2 in live checks via runner flags
  enforced during the run, not only at preflight. Every limiting flag
  is enforced inside the run.
- Tools returning `tool_not_configured` are not offered again.
  Epicure pairing tools share one backend: one such failure excludes
  the family.
- Epicure is queried by default, including for specific dish requests.
  Skips need an allowlisted reason (`simple_technique_question` with no
  pairing cue, or `epicure_not_configured` confirmed by config or a
  session outcome, recorded as degraded). Consulted finishes need
  model lines naming returned pairings.
- Web pages are external discovery evidence only: bounded excerpts,
  URLs, titles, timestamps. Accepted refs come from provider citation
  metadata, never from generated JSON. No source text actually
  obtained exists, so numeric claims fail closed. `web_answer` points
  at pages, never claims verified quantities or safety instructions.

## Validation as the last gate

The model proposes; `agent/validate.py` disposes, without a model call:
sourced IDs via exact `(dataset_id, source_id)` lookup; session
retrieval (options need same-session search/get, quantities and plans
need get_recipe); no invented quantities; adaptations labelled;
hard-constraint keys honored; minimum dietary check (conservative term
lists, ambiguous stays unverified); unnamed-restriction block plus
allergen avoidance in three tiers (violated drops, unverified keeps
but lists, else no_listed_terms_found, never "safe"); plan source
equals selected dish plus minimum plan evidence (ingredient-only
sources marked model_adaptation, raw protein needs a food-safety
technique ref); technique refs resolve and were returned in session;
web refs equal session source URLs. Failures feed back once, then stop.
Model text is never rendered as HTML; citations open with
`rel="noopener noreferrer"`.

## What a framework would replace and what it would not

A framework such as LangGraph would replace the manual step loop,
parallel-call fan-out, history capping and retry bookkeeping. It would
not replace: the Postgres session schema and CAS semantics; the typed
tool registry with server-set timeouts and cost classes; the
backend permission and slot enforcement; the Epicure-by-default policy
and skip allowlist; the deterministic validators as the last gate; the
spend ledger with pre-call reservations and breach stops; the
append-only event log and review projections. Those are product and
safety boundaries, not orchestration.

## Consequences

The loop terminates within configured limits without silently
relaxing constraints; session state survives a restart; disabled
search cannot be invoked through the backend; searches produce source
references, logs and gap candidates. Offline results measure the
system, not model judgement; model quality is measured only live.
Tightening budgets or changing the Epicure default needs owner
approval with live evidence.

## Later changes (hardening and D0, 2026-10-07 to 2026-10-11)

Each change followed a classified live or review failure and ships with
tests; the offline harness pins them (v14, 61 cases,
`evals/phase7_agent/results.json`).

| Change | Where | Tests |
|---|---|---|
| A tool batch larger than the remaining calls runs its affordable prefix, then the model gets one tool-less finishing turn instead of an immediate stop (H4). Decision step 4 above described the earlier immediate stop | `agent/loop.py:2618-2628` | `tests/test_h4_budget_recovery.py` |
| A third identical call gets the same one tool-less finishing turn before `agent_no_progress` (after H8 attempt 1) | `agent/loop.py:2629-2637` | `tests/test_stall_recovery.py` |
| A rejected finish on a final turn gets no retry and stops with the budget or stall reason plus a deterministic list of results already held | `agent/loop.py:5075-5086` | `tests/test_h4_budget_recovery.py::test_finishing_turn_rejected_stops_with_list_once` |
| One `agent_turn` decision event per turn: offered and withheld tools with reasons, remaining budgets (H7) | `agent/loop.py:2023` | `tests/test_h7_decision_logging.py` |
| Clipped or omitted directions are read with ranged `get_recipe`; a plan must read or name every omitted index (H3) | `tools/search_tools.py:83-104`, `agent/validate.py:1847-1859` | `tests/test_h3_clipped_directions.py` |
| Plan amounts tied to source ingredients (H2), with list attribution and equipment sizes refined after H8 attempts 3 and 4 | `agent/plan_quantities.py` | `tests/test_agent_validate.py`, `tests/test_validator_regressions.py` |
| A `model_adaptation` plan may not claim fidelity to the source (H3 part 2) | `agent/validate.py`, `fidelity_claim` | `tests/test_agent_validate.py` |
| The plan run's input carries the selected recipe; after a plan, follow-ups get a technique answer, not a re-issued plan (after H8 attempts 2 and 3) | `agent/loop.py:1484-1516` | `tests/test_agent_loop.py` |
| After a finished plan, a request for a different dish may finish with options: `plan -> recommend` is allowed, the selection and plan are cleared, the earlier plan is kept as `superseded_plan` (D0) | `domain/sessions.py:57-69` | `tests/test_agent_loop.py::test_new_dish_after_plan_returns_options` |
| Advice to follow a doneness cue of the source is not a fidelity claim (D0) | `agent/validate.py`, `_cook_advice` | `tests/test_agent_validate.py` |
| Start-up logs the session limits and warns above the `make demo` values (D0) | `api/app.py:95`, `config.py:342-351` | `tests/test_demo_limits.py` |

Milestone 4 (ADR 0003, accepted 2026-10-09) keeps this loop and adds
answer kinds, provenance and an ingredient contract on top of it; it
does not introduce a framework.

