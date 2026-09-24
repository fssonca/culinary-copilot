# Backend clarification guide: hybrid question planning

Backend-only (no frontend). A future UI presents one question group one
question at a time; the API returns the ordered group plus revisions.

## Architecture

```text
client
  -> POST /api/v1/clarification/groups (message + optional CookingRequest)
     domain/rule_planner.py      pure, offline: proposes rule questions
     services/hybrid_planner.py  at most ONE bounded LLM call per group
     services/store.py           bounded in-memory dev store (see below)
  -> POST .../groups/{id}/answers (deterministic, never calls the LLM)
  -> GET  .../groups/{id} (state + pending questions)
  -> POST .../groups/{id}/replan (explicit bounded follow-up, max 5)
```

- `domain/clarification.py` — typed contracts: `Question`, `QuestionGroup`,
  `AnswerPayload`/`AnswerRecord`, `CookingRequestState`, `PlanningOutcome`
  (`needs_clarification` / `ready_for_retrieval` / `blocked`).
  `CookingRequest` is preserved unchanged; state wraps it.
- `domain/rule_planner.py` — deterministic planner (no I/O): required
  questions first, priority order, configurable bound (default 6, max 12).
- `llm/client.py` — application provider boundary: `FakeApplicationProvider`
  for offline tests, `OpenAIApplicationProvider` (`AsyncOpenAI` +
  `responses.parse` structured output). One retry owner: SDK `max_retries=0`,
  the module retries only timeout/connection/rate-limit/5xx up to
  `LLM_APP_MAX_RETRIES`.
- `services/hybrid_planner.py` — prompt building (bounded) + proposal
  validation (dedupe, confirmation-gated corrections, substitution checks).
  A model `is_correction` flag or confidence score is never sufficient
  evidence to replace an explicit value: inferred corrections produce a
  confirmation question plus a `pending_confirmations` record, and the
  previous value and history stay intact until the user confirms through the
  typed answer endpoint.
- `services/answers.py` — deterministic answer validation + state updates.
- `services/clarification_service.py` — orchestration (rule + one LLM call,
  bounded evidence retrieval in worker threads).
- `obs/clarification.py` — structured planning events (no keys, prompts, or
  conversations).

Verified against current official OpenAI docs (2026-09-23): Responses API
structured outputs via `responses.parse(..., text_format=Model)` returning
`output_parsed`, with detectable refusals and `status="incomplete"` handling
(<https://developers.openai.com/api/docs/guides/structured-outputs>).

## State ownership

Minimal replaceable boundary (`services/store.py: ClarificationStore`): a
bounded (512 requests, FIFO), threading-lock-guarded, process-local
`InMemoryClarificationStore`. Accepted limitations: restart loss,
single-worker scope, no durability. No PostgreSQL tables or migrations were
added. Client state is never trusted: the server is authoritative and rejects
stale `request_revision`/`group_revision` pairs with 409.

Concurrency contract (single process, enforced with snapshots and
compare-and-swap): reads return deep copies, so mutating a returned object
can never affect storage; answer submissions commit only when the stored
revisions still match the caller's snapshot (two same-revision submissions
cannot both succeed — one wins, the other gets 409); replans commit only
when no newer write landed while planning, so a stale replan returns 409
(`replan_stale`) instead of overwriting answers submitted during its provider
call. No lock is held across network calls. A replan supersedes pending
submissions: it advances the request revision, and in-flight same-revision
writes then fail closed with 409. A per-request registry of every issued
question keeps typed edits to earlier questions working after the pending
group is filtered.

Field status per allowlisted target: `unknown` / `provided` /
`no_preference` (explicit) / `skipped` / `conflicting`. An empty list is not
proof of "no restrictions"; silence never implies a dietary restriction.
Answer history is append-only: edits and invalidations append records.

"Ready for retrieval" means sufficient search information for recipe
discovery (dish direction OR available ingredients, no conflicts, plus
task-essential information such as portions for scaling). It does
not mean safe for generation, nutritionally verified, complete, or scalable.
Every response carries a `readiness_note` disclaimer and an
`unenforced_constraints` list: dish direction feeds the query text,
ingredients filter candidates, and `time_minutes` caps duration, but cuisine,
preferences, dietary constraints, equipment, and substitution choices are
preserved in state (`state.values`, `state.field_status`, and the downstream
`state.request` copy) without being enforced by the current search — they
must be checked downstream, never treated as satisfied. Required unresolved
conflicts (`conflicting_*` blockers, derived from state on every round-trip)
or missing task-essential information prevent readiness; unanswered dietary
information stays `unknown`, distinct from explicit `no_preference` and from
`skipped`. Portions are required only for scaling; discovery never requires
portions or a full preference questionnaire.

Custom-text rule: `custom_text` SUPPLEMENTS selections (both retained);
custom-only answers are allowed where `allow_custom_text` is set. Reserved
none-ids (`none`/`no_preference`/`no_restriction`) are mutually exclusive
with other selections.

## Question/answer JSON examples

Initial group (vague request):

```json
{
  "request_id": "req-a1b2c3",
  "group_id": "grp-d4e5f6",
  "request_revision": 1,
  "group_revision": 1,
  "outcome": "needs_clarification",
  "ready_for_retrieval": false,
  "planning_mode": "rule_only",
  "questions": [
    {
      "id": "rule-dish-direction",
      "semantic_key": "dish_direction",
      "topic": "Dish direction",
      "prompt": "What would you like to cook? (a dish, craving, or meal idea)",
      "input_type": "text",
      "options": [],
      "allow_custom_text": false,
      "required": true,
      "priority": 90,
      "depends_on": null,
      "source": "rule",
      "target": "dish"
    }
  ],
  "answered": [],
  "skipped": [],
  "invalidated": [],
  "blockers": [],
  "versions": {"planner": "1", "schema": "1"},
  "counts": {"rule": 6, "llm": 0}
}
```

Answer submission:

```json
{
  "request_revision": 1,
  "group_revision": 1,
  "answers": [{"question_id": "rule-dish-direction", "text": "chicken curry"}]
}
```

Confirmation flow (model-inferred correction to an explicit value):

```json
// group contains (source llm, required false, priority 95):
{
  "id": "llm-confirm-confirm_dish-1",
  "semantic_key": "confirm_dish",
  "topic": "Confirm dish change",
  "prompt": "You set dish to 'ramen'. The message suggests 'pizza'. Should I update it?",
  "input_type": "single_choice",
  "options": [
    {"id": "keep_current", "label": "Keep: ramen"},
    {"id": "confirm_change", "label": "Update to: pizza"}
  ],
  "target": "dish"
}
// group also carries:
{
  "pending_confirmations": [
    {
      "target": "dish",
      "current_summary": "ramen",
      "proposed": "pizza",
      "question_id": "llm-confirm-confirm_dish-1",
      "status": "pending",
      "relaxes_constraint": false,
      "quote_supported": true
    }
  ]
}
// resolve through the typed endpoint (nothing changes until this call):
{"request_revision": 2, "group_revision": 2,
 "answers": [{"question_id": "llm-confirm-confirm_dish-1", "selected": ["confirm_change"]}]}
```

Substitution question (LLM, uncertainty preserved):

```json
{
  "id": "llm-cream_alternative-1",
  "semantic_key": "llm_cream_alternative",
  "topic": "Cream alternative",
  "prompt": "Which cream alternative do you have available?",
  "input_type": "single_choice",
  "options": [
    {"id": "oat_cream", "label": "Oat cream"},
    {"id": "soy_cream", "label": "Soy cream"}
  ],
  "source": "llm",
  "target": "substitution_choice",
  "required": false,
  "substitution_evidence": {
    "status": "unverified",
    "verified": false,
    "note": "Compatibility unverified."
  },
  "evidence_backed": false
}
```

## Endpoint usage

```sh
# Create a group from a message and/or structured request.
curl -X POST http://localhost:8000/api/v1/clarification/groups \
  -H 'Content-Type: application/json' \
  -d '{"message": "plan dinner", "use_llm": false}'

# Submit answers (or edits) — deterministic, no LLM call.
curl -X POST http://localhost:8000/api/v1/clarification/groups/<grp>/answers \
  -H 'Content-Type: application/json' \
  -d '{"request_revision": 1, "group_revision": 1,
       "answers": [{"question_id": "rule-dish-direction", "text": "ramen"}]}'

# Retrieve current state + pending questions.
curl http://localhost:8000/api/v1/clarification/groups/<grp>

# Explicit follow-up group (bounded; at most one LLM call).
curl -X POST http://localhost:8000/api/v1/clarification/groups/<grp>/replan \
  -H 'Content-Type: application/json' -d '{"message": "something sweet"}'
```

Error behavior:

| Situation | Status | Note |
| --- | --- | --- |
| Invalid answer / option id / malformed payload | 422 | clear message, no state change |
| Stale `request_revision`/`group_revision` | 409 | resubmit against current revisions |
| Concurrent submission lost the race | 409 | same as stale: exactly one same-revision write wins |
| Stale replan (answers landed while planning) | 409 | `replan_stale`: refetch and replan again if still needed |
| Unknown group/request | 404 | `clarification group not found` |
| Provider timeout/refusal/incomplete/schema failure | 200 degraded | rule-only group + `provider.error` code |
| Replan budget exhausted (>5) | 422 | explicit bound |

Responses never expose raw prompts, secrets, chain-of-thought, or internal
validator details. Usage tokens stay `null` (unknown) when unreported, never
zero.

## Enablement and provider failures

Application LLM settings (`LLM_ENABLED=false` by default) are separate from
`LLM_INGESTION_ENABLED`. The disabled path makes zero provider calls and
reports free-text interpretation as unavailable (structured state only).
When enabled, failures degrade to rule-only planning with a `provider.error`
code (`timeout`, `rate_limited`, `auth_failure`, `provider_unavailable`,
`refusal`, `incomplete`, `schema_failure`); only retryable transport errors
are retried, at most `LLM_APP_MAX_RETRIES`. Client startup/shutdown is
managed in the app lifespan. No live model calls occur in default tests
(`FakeApplicationProvider` + synthetic evidence).

## Group planning vs serial presentation

The backend plans a prioritized bounded GROUP (default 6); the future UI
presents it ONE question at a time in order (required first, then priority).
Dependencies (e.g. dietary detail depends on the dietary screen) are
declarative data. Answering reevaluates dependents: inapplicable questions
are removed and dependent answers invalidated (history retained). Skipped
required questions become documented `blocked` blockers (with a scope-change
hint), never repetition loops. Unresolved confirmation questions are carried
into follow-up groups so pending confirmations stay answerable.

## Substitution safety

Model-generated substitution options are contextual choices, not universal
equivalences. Proposals are checked against known ingredients, equipment,
and hard constraints (dietary/allergy); conflicts are recorded and
`verified` stays `false` without server-retrieved canonical recipe evidence
(dataset-qualified `(dataset_id, source_id)` via the existing repository, in
worker threads). Evidence status distinguishes `not_queried` / `unavailable`
/ `queried_no_result` / `backed_by_evidence` / `unverified`. A model claim
alone never establishes allergy safety, dietary compliance, equivalence, or
quantity conversion. An Epicure hint helper exists, but the current planner passes an empty
ingredient, so it does not query pairings. The separate pairing endpoint works
when enabled; no new Epicure operators were added.

## Known limitations and next steps

- In-memory store: restart loss, single worker, 512-request cap. Cross-thread
  safety holds within one process (snapshot/CAS); multi-worker deployments
  need the future persistent store.
- Model-inferred updates to not-yet-known fields apply only when
  high-confidence, quote-grounded in the current message, and shape-valid;
  all other inferences stay uncertain or become confirmation questions.
- No embeddings, vector retrieval, or agent loop. Generation
  exists only as the grounded recommendation workflow
  ([docs/recommendations.md](recommendations.md)), which consumes, but
  never changes, clarification contracts. Streaming is implemented on the
  recommendation path only
  ([walkthrough](phase4-streaming-walkthrough.md)); clarification itself
  is not streamed.
- Planning telemetry is complete per request (Phase 4): every group
  creation and replan emits one `clarification_planned` event with the
  real group id (rule-only, provider-unavailable, provider-exception,
  and LLM-assisted paths), message length only, and no message text.
- Conflict detection is keyword-based (veg + meat tokens); richer
  constraint reasoning is future work.
- Retrieval integration (Phase 1, implemented and repaired): the current
  revision-specific ready group connects to bounded evidence summaries
  through `POST /api/v1/retrieval/search` while preserving unenforced
  constraints; superseded groups get a 409 naming the current group (older
  groups stay readable history); see [the retrieval guide](retrieval.md).
  The evaluation baseline itself (frozen labels, measured Recall@5/MRR)
  remains Phase 2 work, gated on label calibration and the approved
  Food.com search rebuild. Persistence and frontend work are deferred.

## Architecture diagrams

See the [system overview](architecture/README.md) and
[request and concurrency flows](architecture/request-flows.md). The overview
documents current wiring limitations, including title/ID-only recipe context
and the inactive Epicure hint call within clarification.
