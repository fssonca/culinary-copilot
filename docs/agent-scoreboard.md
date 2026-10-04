# Agent scoreboard (Milestone 3, Phase 7 part 1, offline)

Status: offline harness v2 complete 2026-10-04. No model calls, no
embeddings calls, no web requests, no downloads. Writes went to the
disposable `culinary_check_phase7` database only (dropped afterward).
The live run is part 2 and is not authorized here; see
`evals/phase7_agent/LIVE_PLAN.md`.

v1 (cases sha `5655b1cc…`, 27/27 scored, 4/4 adversarial, kept in
`results.json` as `history_v1`) scored the dietary and adversarial
cases on the stop reason alone. v2 pins the guard evidence itself
(rejection text, forbidden options, constraint_check statuses,
per-option allergy checks, run-final oat check, strict error reasons,
food-safety refs); see `evals/phase7_agent/METRICS.md`. Scorer
mutation tests (`tests/test_phase7_scorer.py`) prove each check bites.

Offline results measure the system (loop control, tools, validators),
not the model's judgement. The provider is scripted per case
(`evals/phase7_agent/cases.json`: synthetic turns, not model output).
Model quality is measured only by the live run.

## Harness

- `evals/phase7_agent/run.py`: one command from the repo root:
  `uv run python evals/phase7_agent/run.py`. Writes `results.json` and
  `epicure_compare.json` into that directory.
- `evals/phase7_agent/cases.json`: version
  `phase7-cases-v2-2026-10-04`, 28 cases, sha256
  `ccff3910a0bfe61b3cd4b3f64a84f4f93e0aff39ca3a69eea49eaa40b8d6ed59`
  (recorded in `results.json` as `cases_sha256`; v1 sha and aggregate
  kept as `history_v1`). A later edit means a new version, never a
  silent change.
- `evals/phase7_agent/METRICS.md`: metric definitions. Latency, tokens
  and cost are "not measured offline (scripted provider)".
- Real agent loop (`agent/loop.py`), real tools (`tools/`) and real
  validators (`agent/validate.py`) in every case. Recipe/ technique
  tools use fakes via `ToolContext.impl_overrides` plus a fake Epicure
  core; `search_web` uses the real backend with a fake sub-request
  provider so permission and slot enforcement are exercised; the
  permission-off backend gate is additionally probed directly with
  `run_tool`.

## Aggregates (28 cases, v2)

- Task completion (scored): 28/28 (rate 1.0). Expected-fail: none;
  both known gaps are fixed (see below).
- Adversarial catch rate: 4/4 (1.0), each with its pinned rejection
  text. Adversarial cases: `p7-epicure-skip-unjustified` ("needs
  Epicure consulted"), `p7-search-invented-url` ("was not returned in
  this session"), `p7-tool-invalid-args`, `p7-invalid-transition-caught`.
- Stop reasons: `agent_sufficient_evidence` 19,
  `agent_validation_failed` 2, `agent_needs_user_input` 3,
  `agent_max_steps` 1, `agent_tool_budget_exhausted` 1,
  `agent_token_budget_exhausted` 1, `agent_wall_clock_exceeded` 1.
- Invalid transitions: 1 total (in `p7-invalid-transition-caught`,
  recovered then finished).
- Tool-argument validity (mean): 0.993. Only
  `p7-tool-invalid-args` is below 1.0 (0.8: 1 invalid of 5 calls,
  recovered then finished).
- Unnecessary-call rate (mean): 0.0. No forbidden calls.
- Epicure compliance: 27/28 (0.964). The single non-compliant case is
  the adversarial unjustified skip, correctly caught.
- Source-reference correctness: 28/28 (1.0).
- Unsupported-claim cases: 0 (offline notes are deliberately plain;
  most carry no checkable claims, which marks the system check, not
  model quality).
- Latency, tokens, cost: not measured offline (scripted provider).

## Case list (criteria in `cases.json`)

- `p7-epicure-default` (Epicure queried by default): pass.
- `p7-epicure-skip-justified` (simple_technique_question,
  technique_answer): pass.
- `p7-epicure-skip-unjustified` (adversarial unjustified skip):
  caught (`agent_validation_failed`).
- `p7-epicure-degraded` (unavailable, degraded options): pass.
- `p7-ask-missing-ingredient` (yogurt ask, answer, resume): pass.
- `p7-ask-allergy-peanuts` (unnamed allergy, answer peanuts, resumed
  options allergen-checked; Phase 3 closure item): pass.
- `p7-select-plan-cited` (options, select, plan cites selected with
  safety ref): pass.
- `p7-plan-raw-protein-safety` (raw-protein plan carries food-safety
  technique ref): pass.
- `p7-search-off-refused` (permission off: not offered in loop, plus
  direct backend probe refuses `tool_permission_denied`): pass.
- `p7-search-on-cited` (permission on: cited web_answer from provider
  evidence): pass.
- `p7-search-invented-url` (adversarial invented URL): caught
  (`agent_validation_failed`).
- `p7-search-limit-reached` (per-session limit 1, second search
  refused `search_budget_exhausted`, answered from first): pass.
- `p7-vegan-conflict` (vegan, chicken dropped, lentil survives): pass.
- `p7-allergen-conflict` (peanut answer, peanut option dropped): pass.
- `p7-oat-milk-gluten-gap` (oat milk unverified for gluten,
  scored on the run final): pass.
- `p7-mixed-fraction-gap` ("1 1/2" vs source "1.5"): pass after the
  exact-rational fix.
- `p7-empty-honest` (empty retrieval, honest question, no invented
  recipe): pass.
- `p7-tool-timeout` (timeout, then recovered): pass.
- `p7-tool-invalid-args` (adversarial invalid args, then recovered):
  caught and recovered.
- `p7-tool-unavailable` (transient unavailable, then recovered): pass.
- `p7-budget-steps` (`agent_max_steps`): pass.
- `p7-budget-tools` (`agent_tool_budget_exhausted`): pass.
- `p7-budget-tokens` (`agent_token_budget_exhausted`): pass.
- `p7-budget-wallclock` (`agent_wall_clock_exceeded`): pass.
- `p7-restart-survives` (fresh store/app on same disposable DB,
  resume): pass.
- `p7-tq04-chicken-temp` (full-text miss, honest question): pass;
  retrieval miss recorded.
- `p7-tq15-pink-chicken` (both miss, thermometer question): pass;
  retrieval miss recorded.
- `p7-invalid-transition-caught` (adversarial illegal `move_to`,
  feedback then corrected finish): caught and recovered.

Code pointers: loop `src/culinary_copilot/agent/loop.py`, validators
`src/culinary_copilot/agent/validate.py`, tools
`src/culinary_copilot/tools/` (`registry.py`, `stub_tools.py` for
`search_web`), harness `evals/phase7_agent/run.py`.

## Fixed gaps (both closed in v2, with tests)

- `p7-mixed-fraction-gap` (P7-MIXED-01, fixed): code was wrong. The
  model claims amount "1 1/2" cup for flour; the source stores "1.5"
  cup. `_numbers_equal` compared via float equality, so "1 1/2" never
  parsed and the option was rejected. Fix: exact rational comparison
  reusing the deterministic ingestion parser
  (`recipes/normalize.py::quantity`), not a third parser and not float
  tolerance. "1 1/2" == "1.5" == "3/2"; "0.33" still does not match
  "1/3"; malformed text does not match; units still match exactly.
  Tests: `test_quantity_matching_mixed_fractions` in
  `tests/test_agent_validate.py`. No validation weakened: only true
  numeric equivalents now match.
- `p7-oat-milk-gluten-gap` (P7-OAT-01, fixed): code was wrong. "oat
  milk" did not match the wheat/gluten unverified term "oats".
  Fix: the term list now carries "oat" (covers oat, oats and oat milk)
  instead of "oats". No validation weakened: only adds unverified
  flags, never removes a violation.
- Oatmeal follow-up (review, fixed): "oatmeal" is a single token, so
  word-boundary matching missed it (`no_listed_terms_found`). Fix:
  "oatmeal" added to the wheat/gluten unverified terms. Compound oat
  forms checked against the Epicure vocabulary (1790 entries): only
  `oat` and `oat_milk` contain oat, both covered by "oat". "oat flour"
  keeps its alternative-flour exemption and is unverified via "oat":
  it is not wheat but can be gluten cross-contaminated, so a violated
  reason ("contains wheat/gluten") would be false. "goat cheese" stays
  clean (word boundaries hold). Tests: `test_allergen_oat_compounds`
  (oatmeal and oat flour unverified, plain flour violated, goat cheese
  clean).

## Budget evidence for P3-L-12 (measure; do not change)

Current budgets (unchanged): 8 steps, 12 tool calls, 30k input / 12k
output tokens, 90 s wall clock per run.

Cooperative end-to-end trajectory lengths (steps / tool calls, summed
across the case's runs):

- `p7-epicure-default` (options only): 2 / 4.
- `p7-ask-missing-ingredient` (ask, resume, options): 4 / 4.
- `p7-select-plan-cited` (options, select, plan): 4 / 6.
- `p7-plan-raw-protein-safety` (options, select, plan): 4 / 5.
- `p7-restart-survives` (ask, restart, resume, options): 4 / 4.

A full request, options, select, plan flow fits in 4 steps and 5-6
tool calls offline (scripted). The 8-step / 12-call budgets leave
about 2x headroom for real-model detours (retries after validation
feedback, extra retrieval or Epicure queries). Token and wall-clock
headroom cannot be measured offline; the live plan prices them from
the runner's reservation formulas.

Proposed values (for the owner to decide in checkpoint C or part 2;
defaults unchanged): keep 8 steps and 12 tool calls. Rationale: the
offline cooperative flows use half or less; live attempts 6-9 used up
to 7 steps before asking or finishing, and the token-growth final
turn already protects the input budget. Tightening now would risk
cutting real-model recovery turns with no offline evidence; the live
run should confirm before any change.

## Epicure comparison status (P3-L-11, offline only)

Compared offline, cache-only, no download, on the fixed synthetic
list ["chicken", "garlic", "lemon", "rice", "tomato"]
(`evals/phase7_agent/epicure_compare.json`, full tables there):

- chicken balanced: pork, beef, chicken_broth (peanut 4th).
  chicken cooc: garlic, onion, black_pepper.
- garlic balanced: shallot, leek, chive. cooc: chili_pepper, salt,
  tomato.
- lemon balanced: lime, orange, thyme. cooc: orange, mint, rosemary.

Cooc gives more useful cooking companions for chicken (garlic/onion
vs pork/beef/peanut), confirming the P3-L-11 observation. The default
is unchanged (balanced). No recommendation change here; the live plan
leaves any variant choice to the owner.

Assets present locally (`.cache/huggingface/hub/models--Kaikaku--epicure-{core,cooc,chem}`),
CPU path ran without download. Nothing missing.

## Technique food-safety regressions (full-text mode)

- `tq-04` "chicken internal temperature": full-text misses (FDA doc
  says "poultry", never "chicken"). Offline harness mirrors with 0
  hits; agent asks an honest question. Vector finds
  `tech-fda-safe-32` top-1 (d=0.393, Phase 4 record).
- `tq-15` "pink chicken inside": both modes miss; correct behavior is
  a thermometer answer, not a document hit. Offline harness mirrors
  with 0 hits; agent asks about a thermometer.

The vector comparison needs query embeddings and belongs to the live
plan (priced separately).

## Live run (part 2)

Empty. Prepared plan: `evals/phase7_agent/LIVE_PLAN.md`. Not run. No
commit.
