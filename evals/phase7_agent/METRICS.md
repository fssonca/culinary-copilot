# Phase 7 offline metrics (system, not model judgement)

Offline results measure the system (loop control, tools, validators),
not the model's judgement. The provider is scripted per case: its turns
are written in `cases.json` and carry no judgement. Model quality is
measured only by the live run (part 2, `LIVE_PLAN.md`).

Latency, tokens and cost are reported as "not measured offline
(scripted provider)". The harness records scripted turn sizes for
debugging only and never reports them as real cost or latency.

Honesty limits (close-out): `verify_packet.py` checks the raw live
files against hard-coded expectations, not the packet's narrative —
a passing script means the numbers match, not that prose claims are
complete. Scripted offline cases prove specified behaviour (a given
scripted turn is accepted or rejected); they do not prove a live
model will choose the intended recovery. Every recovery fix below is
marked offline only; not live-evaluated.

## Per-case metrics

- `task_completion`: the expected terminal was reached (stop reason plus
  answer shape, or the expected typed error), plus the v2 guard
  evidence below. Boolean per case. v1 checked the stop reason (and
  shape) alone for dietary and adversarial cases; v2 additionally
  requires the rejection text, forbidden-option absence,
  constraint_check statuses, per-option allergy checks, the run-final
  oat check, the strict error reason and food-safety refs.
- `trajectory_length`: steps (provider turns that consumed a step) and
  tool calls (executed calls) for the case, summed across its runs.
  From `agent_step` events and `tool_call` events.
- `stop_reason`: terminal reason for the case (last run).
- `invalid_transitions`: count of rejected phase moves in the case.
  A model `ask_user`/`finish` with an illegal `move_to` yields
  validation feedback containing "invalid phase move". A terminal
  `invalid_phase_transition` from the API also counts.
- `tool_argument_validity`: share of tool calls without
  `tool_invalid_arguments`. `1 - invalid/total`. No calls counts as 1.0
  (nothing invalid) and is reported with `tool_calls: 0`.
- `unnecessary_call_rate`: calls outside the case's `required_tools`
  plus `allowed_tools`, divided by total calls. Required and allowed
  are declared per case in `cases.json`. Forbidden tools are never
  called in a passing case; any call to a `forbidden_tools` entry is
  reported separately as `forbidden_call: true`.
- `epicure_compliance`: per case boolean. When the case requires
  Epicure consulted, true when session evidence shows an Epicure query
  (successful pairing-tool call in this session or `epicure_outcome`
  set). When the case requires a skip, true when the recorded
  `epicure_skip_reason` is allowlisted and, for `epicure_not_configured`,
  confirmed by configuration or a session tool outcome. Otherwise true
  when the loop's own rule holds (consulted or allowlisted skip).
- `source_reference_correctness`: per case boolean. Every reference in
  a final resolves to evidence returned in that session:
  options to `search_recipes`/`get_recipe` identities,
  quantities and plan sources to `get_recipe` full documents,
  `technique_refs` to `search_techniques` results in the same session,
  `web_refs` to `search_web` source URLs in the same session.
  The harness reuses the same helpers as the loop
  (`recipe_session_evidence`, `session_web_sources`, returned technique
  chunks from `tool_call` events).
- `unsupported_claims`: count of grounding rejections in the case
  (validation feedback naming unsupported pairings or time/temperature
  claims), plus `note_unverified`: whether the final model note passed
  only by containing no checkable claims. The aggregate
  `unsupported_claim_rate` is cases with any grounding rejection
  divided by cases with a final. `unverified_note_rate` is reported
  separately. Offline notes are deliberately plain ("packet finish")
  so most carry no checkable claims; that marks the system check, not
  model quality.
- `adversarial_caught`: per adversarial case boolean. True when the
  expected guard fired (the expected typed error or rejection).
  The aggregate `adversarial_catch_rate` covers adversarial cases only.
- `expected_rejection`: substring that must appear in the run's
  `agent_validation_reject` errors (v2 adversarial validation cases).
  Any other `agent_validation_failed` reason fails the case.
- `forbidden_option_ids`: source ids that must be absent from the
  final options (v2 dietary cases).
- `constraint_status` / `constraint_value`: per-source_id expected
  `constraint_check` status (and value) on the run's own final.
- `dropped_with_reason`: the dropped option must be present in the
  final's `dropped_options` with a reason mentioning the guard.
- `first_rejection_contains`: substring that must appear in the first
  validation rejection (v2 multi-finish cases, e.g. the vegan
  violation on the first finish).
- Allergy resume (`allergen_checked`): every final option needs a
  `constraint_check` entry with the allergen value and a non-violated
  status. Quantity lines alone do not prove the check ran.
- `saw_error` (v2): the specific typed tool reason must appear. No
  generic "error" fallback.
- `needs_safety_ref` (v2): plan `technique_refs` must resolve to
  food-safety technique documents, not merely be non-empty.
- v4 regression cases pin the close-out fixes: `expected_rejection`
  "remove the time" (repeated time claim still fails),
  `first_rejection_contains` for the dropped-claim and model-title
  recoveries and the honored-misuse recovery, stored-title support
  (no rejection at all), and the refetch duplicate (finish on a
  short pointer result).
- `steps_source` (v6, coverage in close-out review): the final
  plan's attribution label must equal the declared value (`source`
  only with fully cited, supported steps covering every stored
  direction; otherwise `model_adaptation`).
- `expect_fetch` (v5): the last logged `get_recipe` call per pair
  must be full or short (duplicate pointer) as declared.
- Web procedural guard (v7, widened in close-out review): verbs
  count as imperative after sentence breaks and after and/then/
  first/next/now/finally/simply/just/you/we/after-that/commas.
  `p7-web-answer-method` pins rejection of method-like text and
  acceptance of a page description; unit tests pin the boundary.
  Accepts: page descriptions, gerunds ("covers whisking"), single
  serving suggestions, unit-less numbers. Rejects: step sequences,
  verb sequences (the live method text), quantities with units.
  Residual risk: single-verb instructions pass ("First mix the
  batter." alone has one hit, below the two-verb sequence bar), and
  hedged method ("you could whisk, which some cooks fold") may pass
  or fail on wording; "you can X" never matches (the modal
  intervenes). The bar stays at sequences to keep page descriptions
  passing.
- `latency_tokens_cost`: string constant "not measured offline
  (scripted provider)" for every case and the aggregate.

## Aggregate metrics

- `task_completion_rate`: completed / total.
- `expected_fail`: known-gap cases (`p7-oat-milk-gluten-gap`,
  `p7-mixed-fraction-gap`) are tracked separately. They are expected
  to fail until fixed and do not count against the pass rate; the
  scoreboard names the side at fault (case or code) with evidence.
- `stop_reason_distribution`: counts per stop reason across cases.
- `mean_trajectory_length`: mean steps and mean tool calls, plus the
  cooperative end-to-end subset (request, then options, then select,
  then plan) reported separately for the P3-L-12 budget evidence.
- `invalid_transitions_total`: sum across cases.
- `tool_argument_validity_mean`: mean of per-case validity.
- `unnecessary_call_rate_mean`: mean of per-case rates.
- `epicure_compliance_rate`: compliant / total.
- `source_reference_correctness_rate`: correct / cases with a final.
- `unsupported_claim_rate` and `unverified_note_rate`: as above.
- `adversarial_catch_rate`: caught / adversarial total.
- `latency_tokens_cost`: "not measured offline (scripted provider)".
