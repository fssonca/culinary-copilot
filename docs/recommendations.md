# Phase 3 recommendations: grounded selection + Epicure (backend only)

**Phase 3 accepted on 2026-09-24** as a bounded backend milestone, based on
AI-assisted review accepted by the owner. This is not independent culinary
verification. See [closure and limitations](phase3-closure.md) and the
[owner decision](../evals/phase3_review/owner_decisions.json).

The current contract uses typed propositions with server-rendered wording and
a snapshot-checked, fully budgeted native-tool continuation. Both paths were
exercised in the two-case live follow-up. Source review and quarantine tests
are complete. Source consistency is not automatically checked: Food.com
000322 remains presentable because its proposed defect has not been applied.

Diagnostics (additive, never sent to the model): `evidence.offered_candidates`
(label, identity, title, evidence fingerprint; also on failure details),
`evidence.tool_continuation` (call id, fetched snapshot, turn-2 item
kinds/ids, sizes), and per native turn `tool_call_envelope` and
`continuation_items` (types/ids only; reasoning content, summaries, and
encrypted content are never recorded). The runner adds a structural
published-claim check and `--stop-on-failure`.

## Entry point

```sh
# 1. Create / clarify a group until ready_for_retrieval is true (see
#    docs/clarification.md for the group/answer flow).
curl -X POST http://localhost:8000/api/v1/clarification/groups \
  -H 'Content-Type: application/json' \
  -d '{"message": "chicken dinner", "dish": "thai chicken curry",
        "request": {"ingredients": ["chicken"]}, "use_llm": false}'

# 2. Request a grounded recommendation (revision-pinned).
curl -X POST http://localhost:8000/api/v1/recommendations \
  -H 'Content-Type: application/json' \
  -d '{"group_id": "<grp>", "request_revision": 1, "group_revision": 1}'

# 3. Opt-in tool mode (default off): bounded get_recipe demonstration.
curl -X POST http://localhost:8000/api/v1/recommendations \
  -H 'Content-Type: application/json' \
  -d '{"group_id": "<grp>", "request_revision": 1, "group_revision": 1,
        "tool_mode": true}'
```

Request body: `group_id`, `request_revision`, `group_revision` (required),
optional `limit` (1–5, default 3, capped by `REC_CANDIDATE_COUNT`),
optional `dataset_id` (supported dataset; omitted = combined corpus),
optional `tool_mode` (default `false`).

## Outcomes (HTTP 200) vs errors

| Situation | Status | Body |
| --- | --- | --- |
| Successful selection | 200 | `outcome: "recommendation"` + server-rendered recipe |
| Not ready / conflicting / blocked | 200 | `outcome: "clarification"` + reason + blockers, no recipe |
| No admissible compatible source | 200 | `outcome: "insufficient_evidence"` + reason + unverified discovery pointers (`ready_to_cook: false`, `why`, `source_defects`) |
| Malformed input | 422 | stable error envelope |
| Unknown group | 404 | `clarification group not found` |
| Stale revisions, superseded group, or mid-execution edits | 409 | refetch and retry |
| Disabled generation, corpus/provider unavailable, auth failure, rate limit, or resource-not-found | 503 | reason `generation_disabled` / `corpus_unavailable` / `provider_unavailable` / `provider_auth` / `provider_rate_limited` / `provider_not_found` |
| Provider timeout | 504 | reason `provider_timeout` |
| Invalid/incomplete output or rejected proposal/request | 502 | stable `error.reason` (below) |

502 reason codes: `schema_failure` (detail lists only error locations
and pydantic error types; rejected model text is never echoed),
`validation_rejected` (with
`detail.validation_reason`: `unknown_identity`, `bad_reference`,
`step_coverage`, `incomplete_source`, `hard_constraint_violation`,
`prompt_injection_detected`, `evidence_changed`; unknown identities
persist the proposed value only when it is identifier-shaped, otherwise
a redaction marker, plus the offered identities),
`input_budget_exceeded` (a turn's complete serialized request cannot
fit `LLM_REC_MAX_INPUT_CHARS`; raised before that turn is sent, with
required/limit characters, UTF-8 bytes, and earlier turns' usage),
`truncated_incomplete_response`
(with `detail.incomplete_reason`, token usage including `reasoning_tokens`,
and per-attempt metadata), `empty_response`, `invalid_tool_call`,
`turn_limit_exceeded`, `provider_refusal`, `provider_content_filter`,
`provider_bad_request` (HTTP 400: client/config error, never retried),
`provider_request_error` (other provider 4xx, never retried, never
collapsed into `provider_unavailable`), and `provider_internal_error`
(local failure before send or during response processing, with the
exception type and a `request_sent` flag; stops the live runner).

**Provider refusal is a controlled failed outcome (502, reason
`provider_refusal`).** It is never mislabeled `insufficient_evidence`,
which is a successful 200 outcome describing the corpus. Conflicting
cooking requirements produce `clarification` (200), never a 409. Disabled
generation fails before any network access (503, zero provider calls).
Failed bodies contain no generated recommendation or fallback candidates
and never expose prompts, secrets, or raw rejected model output.

## Fixed orchestration

Authoritative readiness → early Epicure consultation → retrieval →
complete source fetch → suggestion assessment/model selection →
deterministic validation → server-rendered response.

- Readiness is recomputed from authoritative store state
  (`retrieval_ready` + conflict/blocker checks); caller flags ignored.
- Retrieval ranking is reused unchanged (same query mapping and
  repository search as Phase 1). Complete documents are fetched by exact
  `(dataset_id, source_id)` identity, never legacy fallback lookup.
- Retrieval API summaries are not generation evidence. A bounded internal
  evidence bundle carries untruncated sections (all ingredients with
  quantities/notes, all ordered instructions) plus provenance,
  capabilities, flags, servings, reported durations, and stable
  source-local references (`ing-N`, `step-N`).
- No-truncation budget rule: required fields are preserved verbatim (no
  per-field slicing). Budgeting measures the ACTUAL serialized provider
  request (`llm.client.serialized_request`): input items (system prompt,
  request/epicure/verdict sections, candidate blocks; in tool mode the
  metadata message, continuation items, and tool result), tools,
  `tool_choice`, and the structured-output schema exactly as the SDK
  derives it. Whole candidates are reduced until the complete request
  fits both the evidence budget (`REC_EVIDENCE_MAX_CHARS`) and the total
  input budget (`LLM_REC_MAX_INPUT_CHARS`); every turn is re-measured
  just before it is sent. Per-turn request characters and UTF-8 bytes
  are reported in `evidence.request_chars_per_turn` /
  `evidence.request_utf8_bytes_per_turn`. A smaller later candidate can still fit
  after an oversized one is skipped. When nothing recommendable fits, the
  result is `insufficient_evidence` (`evidence_budget_exceeded`) with zero
  provider calls. The final evidence prompt is never truncated.
- Character budgets are not token counts (code points vs billed tokens).
  Live cost reservation uses UTF-8 byte lengths (every token spans at
  least one byte, so bytes are a conservative token upper bound), never a
  chars/4 estimate. Reasoning items continued by id may be expanded
  server-side into input tokens no local serialization can see; the
  runner reserves the turn-1 output cap for them on turn 2.
- Synchronous DB/Epicure work runs off the async event loop
  (`asyncio.to_thread`); no store lock is held across I/O. State and group
  revisions are rechecked before publication; intervening edits fail
  closed with 409.

## Grounding: selection, not rewriting

The model selects one source recipe and cites its `ing-N`/`step-N`
references, plus optional **typed** propositions (reasons and follow-up
questions). It must not supply quantities, units, rewritten
instructions, or any free text: the proposal schemas forbid them
(`extra="forbid"`, enum types, pattern-bounded refs), so smuggled content
fails schema validation (502) instead of reaching the response. Python
assembles all factual content from the stored source; unknown values
render as `"unknown"`.

### Typed propositions

Replaces the free-text `selection_reasons`/`needs` contract, which let a
valid selection publish "Guaranteed allergen-free and safe for everyone.
Double every ingredient to serve eight." (the instruction-override regex
never enforced the no-safety-claims/no-adaptations rule). The model
proposes `reasons: [{type, ingredient_refs}]` and `questions: [{type}]`
(at most 6 each; `ingredient_refs` at most 6, each `^ing-[0-9]{1,4}$`).
`recommendations/propositions.py` checks each type's prerequisites
against authoritative request state and the selected source, then
renders the wording itself.

| Reason type | Prerequisites | Server wording |
| --- | --- | --- |
| `dish_named_in_title` | Request dish present; every dish word (plural-aware) in the selected title; no refs | `The source title "<title>" contains every word of your requested dish. This is a title match, not a judgement that the recipe suits your request.` |
| `uses_listed_ingredients` | Listed available ingredients present; 1–6 distinct refs, each a selected-source ingredient whose name contains every word of a listed item (curated compounds such as "coconut milk" and free-from names such as "gluten-free pasta" never match a plain item) | `Source ingredients matching items you listed as available: <name> (<ref>), …. The source lists <N> ingredient(s), <M> not marked optional; this does not mean you have everything the recipe needs.` |
| `reported_time_within_limit` | Explicit request time limit; usable (finite, positive) source-reported total within it; no refs | `The source reports a total time of <t> minutes, within your <limit>-minute limit. This is the source's own figure, not a promise of how long it will take you.` |
| `stated_yield_matches_portions` | Explicit requested portions; known source yield equal to them; no refs | `The source states a yield of <s> servings, the same as the <p> portions you asked for. Nothing was scaled.` |

| Question type | Prerequisites | Server wording |
| --- | --- | --- |
| `desired_portions` | `portions` status `unknown`; source yield known (the only thing the answer can be compared with; an unknown yield is never a reason to ask) | `How many portions would you like to make? The source states a yield of <s> servings; no scaling is performed.` |
| `time_available` | `time_minutes` status `unknown` | `How much time do you have to cook? A time limit restricts future searches to sources whose reported total time fits within it.` |
| `dietary_restrictions` | `dietary_constraints` status `unknown` | `Do you have any dietary restrictions or allergies? This source has not been checked for them, and your answer would not verify it.` |
| `available_ingredients` | `ingredients` status `unknown` | `Which ingredients do you already have? Listed ingredients help rank future searches; they are not checked against everything a recipe needs.` |

"Missing" means status `unknown`; provided, no-preference, skipped, and
conflicting fields are never re-asked. No question asks the user to
supply facts about the recipe (yield, duration, allergens, nutrition): a
user's answer cannot verify a source. No type exists for dietary,
allergy, nutrition, scaling, or practical-time claims.

Outcome policy: when selection, evidence, and hard-constraint validation
pass, propositions whose prerequisites fail are omitted and recorded in
`rejected_propositions` as `{kind, index, type, code}` (codes:
`duplicate_proposition`, `references_not_permitted`,
`references_required`, `reference_not_in_source`, `reference_duplicate`,
`reference_not_listed_ingredient`, `request_dish_missing`,
`title_lacks_dish_words`, `request_ingredients_missing`,
`request_time_limit_missing`, `source_time_unknown`,
`source_time_exceeds_limit`, `request_portions_missing`,
`source_yield_unknown`, `source_yield_differs`,
`request_field_not_missing`). No free text is echoed. An omitted
proposition never invalidates a valid selection, and a recommendation
may carry no reasons or questions. Schema violations, invalid
selections, missing/invalid references required for selection, and
hard-constraint failures remain controlled failures. The
instruction-override screen still runs over every proposal string as
defense in depth; no keyword blacklist or second LLM reviewer is used.

Response contract (compatible where practical): `selection_reasons` and
`needs` keep their names and `list[str]` type but now hold the server
wording of accepted reasons/questions (never model text);
`selection_reasons_note` says so. New: `propositions.reasons[]`
(`type`, `ingredient_refs`, `text`), `propositions.questions[]` (`type`,
`text`), `rejected_propositions[]`, and in `evidence`:
`request_chars_per_turn`, `request_utf8_bytes_per_turn`,
`budget_unit_note`, `evidence_fingerprint`. Close-out pass: `source_checks`
(see below). Model-side, the wire schema
fields `selection_reasons`/`needs` no longer exist (sending them is a
`schema_failure`).

Validation (deterministic):

- Selected identity is in the supplied evidence (`unknown_identity`).
- References exist, belong to that recipe, and cover every ingredient and
  every step, steps in source order (`bad_reference`, `step_coverage`).
  Index checks establish traceability, not semantic correctness.
- Only `recommendable` sources can be selected (`incomplete_source`).
  Recommendable means: required sections present as non-empty lists AND
  zero omitted entries AND no error-severity source defects. Malformed
  entries are tracked with stable reasons (`entry_not_object`,
  `missing_name`, `entry_malformed`, `section_missing`, `section_empty`)
  and always block recommendation — a source is never admitted merely
  because some entries remain.
- Source fidelity is distinct from recipe completeness: legacy records
  without modern capability metadata are shown faithfully with
  `capabilities_unknown` (explicit unknown, never permission, never a
  block); warnings/info quality issues are preserved as context while
  error-severity defects block with `source_defect_error`. A source with
  honest unknowns (servings, durations, units) can be presented and
  recommended; a source with missing sections, omissions, or blocking
  defects cannot. See
  [Structural admission vs source consistency](#structural-admission-vs-source-consistency).
- Invalid numerics (booleans, non-numeric, non-finite, negative servings
  or amounts) are rejected to explicit unknown per field
  (`servings_invalid`, `amount_invalid`) and never rendered as known.
  Nothing is invented.
- Instruction-override patterns in any proposal string are rejected
  (`prompt_injection_detected`; defense in depth, since typed proposals
  carry no free text).
- The selected candidate must match the fingerprint of the evidence
  snapshot supplied to the model (`evidence_changed` otherwise).
- Server-side rendering prevents model rewrites; it does not certify
  source accuracy.

### Structural admission vs source consistency

`recommendable` (and its legacy mirror `complete`) is **structural
admission**: required sections present, no omitted entries, no recorded
error-severity defect. It is not semantic completeness. Nothing checks
that the ingredient list covers what the steps use: FOLLOWUP-01's source
(Food.com 000322) was admitted although its steps use potatoes, oil and
water that its ingredient list omits.

Bounded policy (close-out pass):

- A **known blocking inconsistency** is one recorded on the stored source
  as an error-severity quality issue (proposed code
  `ingredient_list_inconsistent`). The existing gate then keeps the source
  out of model evidence, so it can never be a ready-to-cook
  recommendation.
- Such a source may appear only as an explicitly unverified discovery
  pointer. Pointers carry `ready_to_cook: false`, `why` (`blocking source
  defect`, `structurally incomplete source`, or `constraint-unverified`)
  and `source_defects` (recorded blocking codes), plus identity and
  provenance; never recipe content. Pointers appear on
  `insufficient_evidence` outcomes only.
- **Unknown quantities alone are not a broken ingredient list.** Unknown
  amounts, missing units, a quantity-count mismatch flag and warning/info
  issues stay context; such sources remain recommendable and render
  unknowns as `unknown`.
- **Automatic detection is not implemented.** No stored row carries the
  inconsistency code today, and nothing infers it. Recording it on a row
  is a data change that needs separate authorization; proposals live in
  `evals/phase3_review/enrichment_proposals.json` (never loaded at
  runtime). The stored 000322 row is unchanged and still presentable.
- Every recommendation discloses this in `source_checks`:
  `structural_admission: "passed"`, `recorded_blocking_defects`,
  `recorded_quality_context`, `source_flags` (as stored),
  `ingredient_list_vs_steps: "not_checked"`, and a note stating that
  structural checks do not establish completeness.
  `constraints_not_verified` still lists `completeness`.

Covered by `tests/test_recommendations_source_consistency.py` (isolated
fixtures; the gate, the pointer labels, honest unknowns, and that no
production module references the review fixtures).

### Quarantine and served rows

Only rows in `recipes` can be served. Retrieval
(`search_all`/`search_recipes`) and exact lookup (`get_recipe`) read that
table alone; nothing on the recommendation path reads
`recipe_quarantine`, which is audit history. Semantics of coexistence:

- A quarantined-only identity is never a candidate; a proposal naming it
  is `unknown_identity`.
- Food.com snapshot import (`import_data.persist`) replaces the dataset's
  rows, so a quarantined row is served only if another accepted row has
  the same id: a `duplicate_id` quarantine row coexists with the first
  accepted row, whose content is served.
- The hybrid loader (`llm_batch.cmd_load`) upserts. A historical
  quarantine entry does not invalidate a later accepted version, and a
  later quarantine entry does **not** retract an earlier accepted row;
  history is retained. Retracting a row needs an explicit data change.
- Exact `(dataset_id, source_id)` identity, including leading zeros, is
  preserved end to end.

Verified by `tests/test_recommendations_quarantine_pg.py` (disposable
PostgreSQL, real loaders, full recommendation workflow with a fake
provider). A read-only check of the application database on 2026-09-24
found no identity present in both tables (12 Food.com and 431 foodie
quarantine rows).

No scaling, substitution, merged recipes, or generated cooking-step
adaptations exist in Phase 3. Epicure ideas appear only as separately
labeled, unverified pairing notes, never inside the executable recipe.

## Constraint policy

Verdicts (`supported` / `violated` / `unresolved` / `not_applicable`) are
computed by `recommendations/policy.py` from source evidence only. Model
assertions cannot promote a hard constraint to supported (the proposal
schema has no verdict field).

- Time ceilings reuse the shared duration policy; only finite positive
  reported totals can satisfy a ceiling. Supported time verdicts are
  labeled source-reported only, never deadline-feasibility promises.
- Dietary hard constraints: a candidate is `violated` only on a
  definitive whole-word (plural-aware) contradiction between a canonical
  ingredient name and the diet. Diet identifiers are normalized
  explicitly (vegan/plant-based; vegetarian/veggie; gluten-free incl.
  glutenfree/celiac spellings); unknown or custom restrictions have no
  check and stay `unresolved`. Curated compound exceptions
  (`coconut milk`, `peanut butter`, plant milks, `rice/almond` flours,
  etc.) and explicit free-from qualifiers (`gluten-free pasta`,
  `vegan cheese`) are treated as ambiguous and left `unresolved`, never
  decided by keyword. Every violation records the matched source
  ingredient reference, the matched token, and an explanation. Keyword
  absence is `unresolved`, never `supported`: "no prohibited keyword
  found" is not dietary compatibility. This is a limited heuristic, not
  dietary or allergy certification.
- A known hard-constraint violation can never yield a successful
  recommendation (violated candidates are excluded from generation
  evidence; selecting one fails validation).
- An unresolved dietary hard constraint yields `insufficient_evidence`
  (`hard_constraint_unresolved`) with explicitly unverified discovery
  pointers: source-evidence gaps are not resolvable by user questions, so
  the system abstains rather than certify. This conservative gate means
  dietary-constrained requests abstain until verified dietary evidence
  exists; the limitation is tracked in the live-evaluation coverage.
- Cuisine, preferences, equipment, and substitution choices are
  `not_applicable` (no enforcement evidence; preserved unverified).

## Provider and limits

`OpenAIApplicationProvider.complete_recommendation` extends the existing
application provider with separate enablement
(`LLM_RECOMMENDATION_ENABLED`, default false) and limits (`LLM_REC_*`),
verified against the installed SDK (openai 3.16.2:
`responses.parse(..., text_format=Model)` returning `output_parsed`,
detectable refusals, `status="incomplete"` with
`incomplete_details.reason`, `usage.output_tokens_details.reasoning_tokens`,
`max_output_tokens`, `reasoning={"effort": ...}`, `timeout`,
`max_retries=0`) and current official docs (Responses structured
outputs; reasoning guide: reasoning tokens billed as output tokens and
count toward `max_output_tokens`; `reasoning.effort` values
`none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`, model-dependent).
Model (2026-09-24): `gpt-6-luna` only, from the registry in
`llm/models.py`; configuration refuses any other model. Its model page
lists Responses, Batch, structured outputs and function calling, and
reasoning efforts `none`/`low`/`medium` (default)/`high`/`xhigh`/`max`
(not `minimal`). Standard pricing per 1M tokens: input $0.10, cached input
$0.01, cache writes $0.125, output $0.50 (both pages verified
2026-09-24). Phase 3 live results were measured on `gpt-5-nano` with
`minimal` effort; they are historical and were not re-run on
`gpt-6-luna`. One retry owner: SDK retries disabled; the
module retries only timeout/connection/rate-limit/5xx up to
`LLM_REC_MAX_RETRIES` (400s, refusals, content filters, incomplete, and
schema failures never retried). Attempts, latency, token usage
(including reasoning tokens), response ids, and per-attempt failure
metadata (per-attempt `request_sent` flags, SDK exception type, HTTP
status, provider request id, incomplete reason) are recorded; unknown
usage/cost stays unknown. Incomplete responses keep their usage and
`incomplete_details.reason` on the raised error into response/attempt
metadata. Only real SDK/transport exceptions become
`ProviderUnavailableError`; local failures become the distinct
`ProviderInternalError` (reached=false unless a request was sent).
Post-provider validation failures carry attempt counts and
`provider_reached: true`.

Conservative defaults and rationale:

| Setting | Default | Rationale |
| --- | --- | --- |
| `REC_CANDIDATE_COUNT` / `REC_CANDIDATE_MAX` | 3 / 5 | Selection quality over recall; small evidence bundle |
| `REC_EVIDENCE_MAX_CHARS` | 6000 | Fits ~3 complete small recipes; overflow abstains |
| `LLM_REC_REASONING_EFFORT` | `none` | Recommendation-only switch (never ingestion's `LLM_REASONING_EFFORT`); the lowest effort `gpt-6-luna` documents (it does not accept `minimal`, the Phase 3 value on `gpt-5-nano`, which produced 0 reasoning tokens); validated against the model at startup; sent explicitly and recorded per case |
| `LLM_REC_MAX_OUTPUT_TOKENS` | 6500 | Measured on `gpt-5-nano` (not yet re-measured on `gpt-6-luna`), not guessed: 5414-char worst-case valid label selection under the historical free-text contract (server-issued 1-char label, evidence-bounded refs, 5×200-char reasons + 5×200-char needs) + 1000-token reasoning allowance (docs floor "few hundred"; minimal effort "few or no" reasoning tokens); covers reasoning + text. Typed propositions shrink the worst case by 1076 chars (≈4338); the cap is unchanged and now more conservative. LIVE-07 observed 224 output / 0 reasoning tokens (observation, not the bound) |
| `LLM_REC_TIMEOUT_S` / `LLM_REC_MAX_RETRIES` | 20s / 1 | Interactive latency; single bounded retry |
| `LLM_REC_MAX_INPUT_CHARS` | 12000 | Total serialized input budget (chars); payloads reduced whole to fit |
| `REC_EPICURE_MAX_INGREDIENTS` / `REC_EPICURE_SUGGESTION_COUNT` | 5 / 5 | Bounded local consultation |
| `REC_MAX_PROVIDER_TURNS` / `REC_MAX_TOOL_CALLS` | 2 / 1 | Default path uses 1 turn; tool mode 2 turns + 1 call |

## Live-evaluation runner

`scripts/recommendations_live/runner.py` replaces the manual procedure.
Dry-run (default) drives the real HTTP workflows against the local
corpus read-only, captures would-be provider payloads, and records
reservations with zero provider calls and zero spend:

```sh
uv run python scripts/recommendations_live/runner.py \
  --cases evals/cases/phase3_live_cases.json --out evals/results/phase3_live
```

Dry-run payload bytes include the structured-output schema (default
path) and the tools/`tool_choice` (tool turn 1), since both are billed as
input. The tool-mode turn-2 reservation is `LLM_REC_MAX_INPUT_CHARS × 4`
bytes (the service refuses to send a larger continuation) plus the
turn-1 output cap for server-expanded continuation items. A failing turn
that was never sent bills nothing; completed turns bill actual usage.

Live mode requires explicit opt-in, a positive `--ceiling-usd`, explicit
`--price-input-per-1m/--price-output-per-1m` (missing pricing blocks
submission), a configured `OPENAI_API_KEY`, and `--model` matching
`LLM_REC_MODEL`. It reserves per-case cost before submission (byte-bound
input tokens, output bound covering reasoning+text, all allowed
attempts), bills $0 for cases that make no provider call (reservation
released) and for free failure reasons (disabled/corpus/auth/not-found/
rate-limited), charges unknown reported usage on completed provider
calls at the full reservation, enforces the aggregate ceiling and stop
rules (unsupported claim, fidelity mismatch, 3 consecutive paid
failures, provider access, spend projection), derives reached/billed
status from recorded attempt metadata (explicit flag, then per-attempt
`request_sent`), bills $0 where no request was sent, stops immediately
on local internal errors, runs serially with write-ahead persistence
(`submitting` recorded before submission, raw response before grading,
grading in a guard that records `grading_error`, `summary.json` in a
`finally` block on every exit including crashes), refuses to resubmit
`submitting` cases on resume (exit 7 `needs-review`; `--retry-failed`
only re-runs unpaid finals), and records per case whether the provider
was reached plus attempt counts/metadata and request ids. Fidelity
re-fetches run through the case repository boundary (labeled synthetic);
out-of-corpus selections are recorded grading failures, never
exceptions. All run traffic executes on one event loop for the whole run
(provider created/started/used/closed there; httpx ASGI transport per
case) — sharing one provider across per-case TestClient portals reused
pooled SDK connections on dead loops, failing alternating calls with
`RuntimeError: Event loop is closed` (runs 1/2/4 root cause). The provider
refuses cross-loop use with a clear controlled error and persists a
bounded redacted message for internal errors. A passing zero-network
`--rehearse-transport` run (real client vs localhost stub: all 10 cases
incl. LIVE-10 tool turns) is the documented precondition for `--live`;
`--rehearse` (SDK stub, incl. failure envelopes) supplements it. See
[the live evaluation plan](../evals/phase3_live_plan.md).

## Epicure (Stage B)

Early consultation uses real canonical ingredient names from
clarification state, through the adapter boundary. Stage A used the fake
adapter; production wires `CachedEpicureAdapter` over the existing
`EpicureCore` with cached assets only (no downloads from this path;
missing assets map to `unavailable`). Recorded outcomes: `consulted`,
`simple_technique_skip`, `disabled`, `unavailable`, `unmapped`,
`insufficient_ingredient_context`. Disabled/unavailable never appear as a
successful consultation or skip.

Simple-technique skip policy (explicit): dish in the documented list
(`toast`, `boiled egg(s)`, `plain white rice`, see
`service.SIMPLE_TECHNIQUE_DISHES`) with no available ingredients skips
consultation with the recorded reason "pairing suggestions add no
selection value."

After evidence is available, suggestions are assessed deterministically:
present in the selected source → `pairing_note`; otherwise `deferred` as
a future adaptation. Suggestions never establish substitutions, dietary
safety, nutrition, or chemistry.

**Recommendations proceed when Epicure is unavailable or disabled**,
with explicit degradation disclosure (`epicure.degraded: true`); all
grounding and constraint gates are retained.

## Tool-calling demonstration (Stage B, native function calling)

Separately opt-in (`tool_mode: true`), default off, same contracts. The
mode uses the provider's native Responses function-call lifecycle with
the single strict `get_recipe` function (`candidate_label` enum mapped by the
server to an exact `(dataset_id, source_id)`) and forced tool choice:

1. Turn 1 exposes the function over bounded candidate metadata only (no
    full documents). The returned native `function_call` (name, JSON
    arguments, `call_id`) is validated: exactly one call, allowlisted
    name, strict arguments, identity in the candidate set.
2. One read-only exact-pair fetch is dispatched; the result re-enters as
    a `function_call_output` item carrying the same `call_id`, budgeted
    whole like any evidence (oversized tool results fail closed, never
    truncated).
3. Turn 2 (repair pass 4) sends: a system item with final-selection
    instructions (`TOOL_FINAL_SELECTION_SYSTEM_PROMPT`) plus server
    request context (time limit, portions, dietary values, fields not
    provided, server verdicts for the fetched candidate, Epicure note),
    replacing the turn-1 "reply with ONLY one native get_recipe function
    call" system item; the retained turn-1 user message (byte-identical);
    ALL first-turn continuation items (reasoning items first, in output
    order, then the `function_call` with its `call_id`, which must be
    present); and the exact `function_call_output`. No tools and no
    tool choice are offered; the schema restricts `candidate_label` to the
    fetched label. No user message follows the tool output, so reasoning
    items since the last user message stay in context (official
    reasoning guide; the function-calling guide shows instructions
    changing between the two requests). Previously `final_system` and
    `final_header` were built and budgeted but never sent, and turn 2 ran
    under the tool-only instruction. A repeated tool request in turn 2 is
    a turn-limit violation, not a new dispatch.
    The complete serialized continuation (instructions, retained context,
    continuation items, tool result, schema) is budgeted before sending;
    a tool result that fits `REC_EVIDENCE_MAX_CHARS` on its own cannot
    bypass a continuation overflow. Evidence is never truncated: an
    overflow stops before turn 2 with `input_budget_exceeded` and turn-1
    usage/response ids preserved.
    Evidence snapshot: the tool fetch rebuilds the candidate and compares
    its content fingerprint (all built fields, not ingredient/step
    counts) with the request's snapshot; a change fails closed
    (`evidence_changed`) before turn 2. The snapshot is what the model
    sees in the tool output, the only candidate the final selection is
    validated against, and what is rendered. Previously validation used
    the earlier candidate while the model saw the refetched one.
4. Chained continuation items use only documented Responses API
    input fields, verified against the reference and enforced by a
    pre-send guard: `function_call` as type/id/call_id/name/arguments
    (`status` is output-populated — the server rejects it on input with
    400 `unknown_parameter`; SDK extras like `async_`, `caller`,
    `namespace`, `parsed_arguments` are undocumented for input and never
    sent), `reasoning` as type/id/summary/content/encrypted_content
    (encrypted content travels when present for stateless continuation),
    `function_call_output` as type/call_id/output. Later-turn failures
    keep earlier turns' usage and response ids in the failure detail for
    per-turn billing (known turns at actual tokens, their full per-turn bound
    for unknown turns).
5. Identity travels as server-issued candidate labels (`"1"`..`"N"` in
    offered order), never raw ids: the selection schema constrains
    `candidate_label` to an enum of exactly the offered labels
    (documented structured-outputs enum support; invalid values are
    rejected by constrained decoding), the `get_recipe` tool takes the
    same label enum (documented strict-mode enum support), and the
    server maps labels back to the exact `(dataset_id, source_id)`.
    Leading-zero IDs and rewrites are structurally impossible;
    comparisons stay exact (no fuzzy or normalized matching).

Absent/multiple calls, malformed arguments, unknown identities, missing
call ids, and turn-limit exhaustion are deterministic 502s. Tool calls,
provider turns, and transport attempts are bounded separately
(`REC_MAX_TOOL_CALLS`, `REC_MAX_PROVIDER_TURNS`, `LLM_REC_MAX_RETRIES`
in the provider module). There is no general agent loop or arbitrary
recipe access, and this mode stays off the normal recommendation path.
Native support is verified against installed SDK 3.16.2 types and
current official docs. The pre-repair flow was exercised live (LIVE-10,
run5_live10b: tool call, label resolution, two-turn selection, but under
the tool-only turn-2 instruction). The repaired turn-2 continuation is
verified offline (tests capturing exact provider input, plus a
zero-network transport rehearsal through the real SDK client) and live
by FOLLOWUP-02 (`evals/results/phase3_followup_live/`). See [Phase 3
closure](phase3-closure.md).

## Limits and non-goals

Single-process in-memory clarification store (restart loss, 512-request
cap) is unchanged. No embeddings, web search, agent loops, or
frontend. Dietary-constrained requests abstain by design (see constraint
policy). Failed responses carry no recipe content; insufficient-evidence
discovery pointers are explicitly unverified.

## Phase 4: streaming and telemetry (implemented 2026-09-24; review fixes applied)

One workflow, two transports. `POST /api/v1/recommendations` keeps its
behavior and body. `POST /api/v1/recommendations/stream` takes the same
request body and serves `text/event-stream` via Starlette
`StreamingResponse` (FastAPI 0.141.1 / Starlette 1.6.0, checked; no new
dependency; `Cache-Control: no-cache`, `X-Accel-Buffering: no`). Both run
`recommendations/service.py::recommend_for_group`; the stream passes an
optional async stage hook (`on_stage(stage, detail)`). No second workflow
copy exists, and entry checks run only inside the workflow.

Event contract `v1` (`recommendations/stream.py`):

- `stage` events: `accepted`, `readiness`, `epicure`, `retrieval`,
  `evidence`, `provider_request` (sent before each provider turn; turn
  number only), `provider_turn` (after a turn completes; turn, attempt and
  tool-call counts only), `validation`, `revision_check`. No model output
  deltas are streamed; the model returns only a label, refs and typed
  propositions, and recipe content is server-rendered after validation.
  There is no `provisional` event.
- Exactly one terminal event: a `final` carrying the same validated body as
  the non-streaming endpoint (`recommendation`, `clarification`, or
  `insufficient_evidence`), or an `error` carrying status/reason/message/
  detail and no recipe content.
- Every event carries `seq` (0, 1, 2, … with no gaps) plus `request_id`,
  `group_id`, `request_revision`, `group_revision`. Keep-alive comments
  (`: keep-alive`) carry no sequence number.

Behavior:

- Pre-stream rule: the endpoint starts the workflow and waits for its
  first stage (`accepted`). Anything the workflow rejects before that
  (unknown group 404, stale or superseded revisions 409, unsupported
  dataset 422, disabled generation or corpus unavailable 503) is an HTTP
  error with exactly the JSON endpoint's status and body. Body-schema
  errors are 422 from FastAPI before the workflow runs.
- After `accepted`, every outcome is one terminal event: mid-run revision
  changes are a 409 `stale_revision` `error`, never a `final`; provider
  failures, timeouts, refusals and schema/validation rejections are one
  `error`; an unexpected exception is a 500 `internal_error` with only
  the exception type in `detail` (no message text).
- Bounds: `REC_STREAM_MAX_EVENTS` (100, including the terminal event; one
  slot is always reserved for it), `REC_STREAM_MAX_DURATION_S` (120 s),
  `REC_STREAM_KEEPALIVE_S` (10 s). The stage queue is bounded and
  lossless: when the client falls behind, the workflow waits at its next
  stage. If the stage budget runs out while the workflow is still running,
  it is cancelled and the stream ends with `stream_event_limit_exceeded`
  (503); if it runs out after the workflow has finished, leftover progress
  stages are skipped, the `final` is still sent, and it carries
  `skipped_stages`. The duration bound cancels the workflow and ends with
  `stream_duration_exceeded` (504); a workflow that already finished
  delivers its result instead.
- Client disconnect: the workflow is cancelled with the reason as the
  cancellation message, whether the endpoint detects the disconnect
  (`request.is_disconnected()` is polled every 0.2 s; ASGI spec 2.4+) or
  Starlette closes the stream itself (spec < 2.4; the generator's cleanup
  cancels with `stream_closed`). asyncio cancellation reaches the
  in-flight provider await (best-effort: `AsyncOpenAI` has no explicit
  abort, and a request already sent may still be billed). Nothing is
  emitted afterwards. Tested through the real endpoint on both ASGI paths.

Telemetry (`obs/recommendations.py`, logging with structured extras, no
new backend). Exactly one `recommendation_completed` event per run,
including cancelled runs and unexpected errors: `request_id`,
`group_id`, revisions, endpoint/transport, outcome and stable reason
(`cancelled` with the cancellation reason; `internal_error:<Type>`),
model, reasoning effort, config/contract/pricing versions, stage timings
(provider stages keyed per turn, e.g. `provider_turn_2`, so tool mode
keeps each turn's latency), total latency, and `turns`: one record per
provider turn with status (`completed`/`failed`/`cancelled`), attempts,
`request_sent`, latency, input/output/reasoning tokens, response id and
tool calls. A provider turn is recorded as it happens, so completed turns
keep their usage after a later failure, validation rejection or
cancellation, and a failing turn keeps whatever usage the provider
reported (for example a truncated response). Privacy: never the message
text, prompts, recipe content, secrets, raw model output, or constraint
values (dietary/allergy); counts and stable codes only.

Cost (`recommendations/pricing.py`, prices from the `llm/models.py`
registry, pricing version `2026-09-24-luna-v1`, keyed by model; there is
no model-independent override): `gpt-6-luna` Standard input $0.10 /
output $0.50 per 1M tokens. `output_tokens` already includes reasoning
tokens, so reasoning is never added again. Totals and
`estimated_cost_usd` are `None` unless every sent turn's usage is known
(`usage_complete`); `known_cost_usd` sums the turns with known usage and
is a lower bound otherwise. Cached-input and cache-write token counts are
not captured, so all input is priced at the list input rate (the $0.01
cached-input discount and the $0.125 cache-write rate are not applied).
Re-verify prices before any live run.

Clarification gaps fixed: planning events emit from `api/clarification.py`
with the real group id for every path (rule-only, provider-unavailable,
provider-exception, LLM-assisted), replacing the `pending` placeholder
and the single LLM-success emission site removed from
`clarification_service.py`.

Tests: `tests/test_phase4_streaming_telemetry.py` and
`tests/test_phase4_review_fixes.py` (regressions for the review
findings, mutation-checked).

Walkthrough: [Phase 4 streaming walkthrough](phase4-streaming-walkthrough.md)
(`curl -N`, generation-disabled, offline). Live smoke (prepared, not
run): [Phase 4 live smoke](phase4-live-smoke.md) (≤2 cases, exact
model, verified pricing, reservation, ceiling, stop rules).
