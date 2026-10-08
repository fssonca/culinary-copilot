# Milestone 3 hardening: prompts for the implementing agent

Companion to `docs/milestone-3-hardening-plan.md`. H0 (`eae6f00`) and H1
(`a8aa69d`) are done. The remaining work is split into seven phases. Each
phase ends at a **STOP**: the agent reports, the owner reviews (with an
AI reviewer if wanted) and commits, and only then is the next phase
given. Phases 5 and 6 do not depend on phases 1 to 4.

| Phase | Plan step | Kind |
|---|---|---|
| 1 | H2 ingredient-aware quantity validation | code |
| 2 | H3 part 1, access to clipped directions | code |
| 3 | H3 part 2, authoritative source/adaptation label | code + UI |
| 4 | H4 budget-exhaustion recovery | code |
| 5 | H5 malformed-record repair preview | offline, data |
| 6 | H6 ranking judgments and regression cases | offline, eval |
| 7 | H7 harness coverage and decision logging | eval + code |

After phase 7: owner checkpoint D, then H8 (live check, owner only).
Neither is given to the agent.

How to use: paste the **Shared instructions** first, then exactly one
phase prompt. Paste the next phase only after the review has passed and
the previous phase is committed.

---

## Shared instructions (paste with every phase)

```text
You are working in the culinary-copilot repository on branch
m3-checkpoint-c-demo-hardening. Before changing anything, read
AGENTS.md, docs/agent.md, docs/architecture/agent-system.md,
docs/milestone-3-hardening-plan.md and the latest sections of
docs/phase7-owner-decisions.md. Check the actual code before relying
on any document, including this prompt; if they disagree, follow the
code and say so in your report.

Scope
- Implement only the phase you are given. Stop when it is done; do not
  start the next phase.
- Preserve unrelated working-tree changes: never reset, clean, stash or
  checkout over them. Leave the untracked
  evals/phase3_agent/live-summary-*.json files untouched.
- Do not commit, push or deploy. The owner reviews and commits.

Forbidden without explicit authorization in the phase prompt
- Paid model or embedding calls, live sessions, model downloads.
- Writes to the application database (container culinary-copilot-db-1)
  or migrations. You may read the application database only inside a
  read-only transaction (SET TRANSACTION READ ONLY). Database write
  tests use disposable databases only.
- Editing .env, migrations 001-007, earlier scenario files, eval case
  versions already used for scored runs, or historical eval results
  (for example evals/results/phase1/baseline_fulltext_post_rebuild.json).
- Committing anything under data/, credentials, model responses or
  database backups.
- Raising budgets. Keep the demo limits (make demo: 40 steps, 40 tool
  calls) distinct from the ordinary configuration (12 steps, 12 tool
  calls).

Engineering rules
- Never weaken validation just to make a test pass. When a test fails,
  decide whether the fixture or the implementation breaks the intended
  contract, and say which in the report.
- Unknown quantities, units, servings, dietary compatibility and
  nutrition stay unknown; never guess them. Recipe identity is
  (dataset_id, source_id).
- Match the surrounding code: comment density, naming, idiom, and the
  dated "why" comments the code already uses.
- Reuse the existing parsers (recipes/normalize.py::quantity,
  recipes/llm_validate.py::canonical_unit); do not add another one.
- Documentation describes a protection by what it actually checks and
  separates implemented behaviour from planned behaviour. Update
  docs/agent.md and docs/architecture/agent-system.md for any
  behaviour change. Committed docs paraphrase model output and never
  quote it.

Checks (run all of them; report the actual output lines)
- make check
- uv run python evals/phase7_agent/run.py: evals/phase7_agent/results.json
  must stay byte-identical unless the phase says otherwise (verify with
  git diff --stat evals/; restore it with
  git checkout -- evals/phase7_agent/results.json after a run).
  Known since H4 (2026-10-08), until Phase 7: case p7-budget-tools
  fails (internal_error, 42/43), so make check reports exactly one
  failure, tests/test_phase7_harness.py. Report any other change; do
  not fix this one before Phase 7.
- uv run python evals/phase7_agent/verify_packet.py

Report (your final message, in this order)
1. Summary: two to four sentences.
2. Files changed: git diff --stat, plus any new untracked files.
3. Decisions: each one with the alternatives you considered and why
   you chose it.
4. Tests added or changed: name each one and the behaviour it pins.
5. Checks: the tail of each check's output (counts, harness line,
   verify_packet line).
6. Counts or measurements the phase asks for, with the exact query or
   script used.
7. Not done, deviations from this prompt, remaining limits and risks.
   If something could not be checked, say so; never claim it works.

If a phase requires a decision that changes a documented contract
beyond what the prompt states, or you find the prompt is wrong about
the code, stop before implementing and report the question instead.
```

---

## Phase 1: ingredient-aware quantity validation (H2)

```text
Phase 1 (plan step H2): make plan quantity validation ingredient-aware.

Current state (verify in the code):
- agent/validate.py: prose_quantities(text), source_quantities(doc)
  and plan_prose_quantity_errors(plan, doc), called from validate_plan.
  source_quantities pools every (value, unit) the recipe states, from
  ingredient entries (amount, unit, original) and from directions. A
  plan amount passes if the pooled set contains it, so the ingredient
  it belongs to is ignored: with chicken 5 1/2 lb and potatoes 1 1/2 lb,
  "1 1/2 lb chicken" and "5 1/2 lb potatoes" both pass.
- agent/loop.py::readable_amount shows the model the source's own
  notation ("5 1/2") when it parses to the same value as the stored
  exact fraction ("11/2").

Required:
1. Tie each mass or volume amount in mise_en_place, steps and plating
   to the ingredient it describes, and check ingredient, amount and
   unit together against that ingredient's source entries (canonical
   name or name, amount, unit, original line). An amount inside a step
   may also match the text of a direction that the step cites.
2. Unattributable amounts (for example "2 cups of the liquid", or an
   amount whose ingredient matches no source entry): decide between
   (a) accepting only when the same amount and unit appear in a
   direction the step cites, and (b) rejecting with a message that
   names the amount. Never accept silently. Document the rule.
3. The rejection message must name the plan text, the amount, and what
   the source states for that ingredient, so the model can correct it in
   its one validation retry.
4. Evaluate the stronger alternative, rendering mise_en_place amounts
   from the validated structured quantities instead of model prose.
   Do not implement it in this phase; write a short recommendation in
   the report (benefits, what breaks, UI impact).

Regression tests (offline):
- swapped ingredients: source chicken 5 1/2 lb and potatoes 1 1/2 lb;
  "1 1/2 lb chicken" and "5 1/2 lb potatoes" are rejected, the correct
  pairing passes;
- notation variants pass: 5 1/2, 5 ½, 11/2, 5.5;
- unit variants pass through canonical_unit (tablespoons, tbsp, Tbsp.);
- total versus per-serving amounts: a per-serving amount the source
  never states is rejected;
- an amount stated only in a direction ("Heat 2 tablespoons oil")
  passes when the step cites that direction and is rejected when it
  does not;
- plural, singular and adjective forms of ingredient names still
  match ("chicken parts" and "cut-up chicken");
- amounts in a model_adaptation step: keep today's rule unless you
  find it unsafe, and state the rule in a test.

Corpus self-check (read-only): build a plan from each recipe's own
ingredient lines and directions and run the new check on all 16,033
recipes. Report how many pass and list up to 20 failures with the
reason. A failure here means the matcher rejects source text, which
must be fixed or justified, not ignored. Say plainly that this check
shows source text passes, not that wrong amounts are caught.

Also measure, on the same corpus, how many recipes have two or more
ingredients with the same (amount, unit), the case the old pooled check
could not tell apart.

STOP after the report.
```

Review focus: ingredient matching cannot be satisfied by any name that
merely contains the word; a false-rejection rate from the self-check;
messages that are usable in the retry; no regression in
`test_agent_validate.py`.

---

## Phase 2: access to clipped directions (H3, part 1)

```text
Phase 2 (plan step H3, part 1): let the model read clipped or omitted
directions before it finishes a plan.

Current state (verify in the code):
- agent/loop.py summarizes get_recipe for the model: at most
  _DIRECTIONS_SHOWN = 12 directions, each cut at _DIRECTION_CHARS = 600
  characters with a trailing "…", and lists cut directions in
  directions_clipped. The task framing explains clipping (search for
  "directions_clipped" in _TASK_FRAMING); read what it tells the model
  to do and change it to match the new behaviour.
- The output limit for get_recipe is in _TOOL_OUTPUT_LIMITS.
- About 184 recipes have a direction over 600 characters and about 421
  have more than 12 directions (re-measure these numbers).

Required:
1. A bounded, typed way for the model to read the omitted text, for
   example optional get_recipe arguments that select a direction range,
   or a dedicated tool. Do not raise _DIRECTION_CHARS,
   _DIRECTIONS_SHOWN or the output limits globally. Each response must
   stay within an output limit and say what is still omitted.
2. Such a call must not count as a repeat of the earlier full fetch:
   check the repeat digest, the duplicate-fetch pointer and the
   stall stop, and add tests for the interaction.
3. A plan finish for a recipe with omitted directions does not succeed
   until either the omitted directions were returned in this run, or
   the plan lists which directions it did not read and labels those
   steps model_adaptation. Feedback on rejection names the directions
   to read and the call to read them.
4. Plan attribution (all directions cited) must count directions read
   through the new path.
5. Tool-call budget: reading the rest costs tool calls. State the cost
   for the worst corpus recipe and confirm it fits the demo and the
   ordinary budgets; if it does not, report that instead of raising
   budgets.

Regression tests:
- a recipe with a 700-character direction: plan blocked until the rest
  is read, then passes;
- a recipe with 14 directions: same;
- a recipe with no clipping: no new requirement, no extra call;
- the new call after a full fetch is not a repeat, but the same range
  twice is;
- the plan-states-unread-directions route passes only with the
  adaptation label.

Measurements (read-only): recipes needing the path, and the
distribution of extra calls needed (1, 2, 3 or more).

STOP after the report.
```

Review focus: no global limit raised; repeat and stall logic still
correct; the extra-call cost is acceptable; the framing text matches
the behaviour.

---

## Phase 3: authoritative source/adaptation label (H3, part 2)

```text
Phase 3 (plan step H3, part 2): make the source/adaptation label
authoritative over the model's note.

Current state (verify in the code):
- agent/validate.py decides steps_source ("source" only when every
  step cites a supporting direction and every direction is cited;
  otherwise "model_adaptation"). When the source has directions, the
  app writes the adaptation note itself. A claim that the source has
  no directions is already rejected; use that rejection as the pattern.
- The UI (src/culinary_copilot/web/js/) badges model_adaptation steps,
  but the model's free-text note is shown as written.

Required:
1. When steps_source is model_adaptation, reject a note, adaptations
   text or plan text that claims the steps follow, match or reproduce
   the source (for example "follows the original recipe exactly").
   Use a deterministic, documented pattern list with negation handling
   (reuse loop.py::_negated_mention or its approach): "does not follow
   the source exactly" must not be rejected.
2. A source-labelled plan is not affected.
3. In the UI, show the source/adaptation label next to the note as
   well as on the steps, from the validated field, never from model
   text. Keep the existing styles and the render.js structure.
4. The rejection message tells the model what to remove, so the one
   validation retry can succeed.

Regression tests:
- a model_adaptation plan whose note claims fidelity is rejected;
- the same claim negated passes;
- a source plan with "follows the source" passes;
- a UI test (the existing Node-backed or DOM-style test pattern in
  tests/test_web_ui.py) shows the label next to the note.

Report the pattern list and the corpus or fixture texts you tested it
against for false positives.

STOP after the report.
```

Review focus: false positives on honest notes; the label comes from
validated data; UI change limited to the label.

---

## Phase 4: budget-exhaustion recovery (H4)

```text
Phase 4 (plan step H4): recover from tool-call exhaustion without
raising limits.

Current state (verify in the code):
- When a batch asks for more tool calls than remain, the affordable
  prefix runs and the excess calls get typed errors; the run then stops
  with agent_tool_budget_exhausted (domain/recommendations.py,
  REASON_AGENT_TOOL_BUDGET). docs/agent.md documents the immediate
  stop. In two 2026-10-07 live sessions this ended the session with
  "start a new session" although usable results were already held.
- Recovery mechanisms already in loop.py: the tool-less wrap-up turn
  after a repeat-only step, the stall stop, and one validation retry.
  Read how the wrap-up withholds tools; reuse that mechanism rather
  than adding a parallel one.

Required:
1. When tool calls run out (or a batch exceeds what remains), give the
   model one finishing turn with no tools, only if the remaining steps,
   input-token ceiling, wall clock and spending limits allow one more
   model call. Check each limit with the same code that enforces it.
2. The finishing turn is offered at most once per run, and its output
   goes through the normal validators (one validation retry rule
   unchanged; state whether the retry is available here and why).
3. If the turn is not affordable, or its finish is rejected, stop with
   a deterministic message that lists the useful results already in
   the session: fetched recipes (titles), options offered, the selected
   dish, the plan if any. Keep agent_tool_budget_exhausted as the stop
   reason unless you justify a new one; check the UI (web/js/outcomes.js)
   and demo fixtures.
4. Never reset or raise session allowances.
5. Update docs/agent.md (stop-reason table and the immediate-stop text)
   and docs/architecture/agent-system.md.

Regression tests:
- a batch larger than the remaining calls, with recipes fetched:
  the finishing turn runs and a valid finish completes;
- the same with steps or token budget also exhausted: no model call,
  deterministic stop listing the results;
- the finishing turn is rejected: stop with the list, no second turn;
- offered at most once per run;
- an existing harness case that ends with agent_tool_budget_exhausted:
  report whether its outcome changes. If results.json would change,
  stop and report instead of editing cases or results.

STOP after the report.
```

Review focus: every limit checked by its real enforcement path; at
most one extra model call; the harness result for the budget case.

---

## Phase 5: malformed-record repair preview (H5, offline)

```text
Phase 5 (plan step H5): find and preview a fix for the malformed
odunola/foodie records. Offline only: no application-database writes.

Facts (verify): about 158 odunola/foodie records are titled "summary",
with ingredients and directions collapsed into one line each. They
appear in search results. The adapter is
src/culinary_copilot/recipes/adapters/foodie.py
(FOODIE_ADAPTER_VERSION = "4").

Required:
1. Read the stored raw text for these records (read-only) and find
   why the adapter mis-parses them. Report the layout difference with
   two or three short examples (paraphrase or truncate the source text).
2. Fix the parser for that layout and bump the adapter version. Keep
   results for correctly parsed records identical. Keep the previous
   version reproducible (follow how earlier version bumps were
   handled).
3. Produce a before/after preview for every affected record: title,
   ingredient lines, directions, parsed quantities. Write it under
   data/ (not committed). Classify each record as repaired, unchanged
   or still malformed, with the reason.
4. Regression: re-parse a sample of at least 500 correctly parsed
   foodie records (and every record if it is cheap) and show no
   change in title, ingredients, directions or quantities.
5. Say whether deterministic reprocessing repairs them without paid
   extraction. If some need extraction, count them; do not run it.
6. Write a proposed re-ingestion procedure for the owner: backup and
   restore check, the order of steps, and the consistency checks for
   recipe identities, provenance, directions, quantities, search
   vectors and embeddings (embeddings may need paid calls; say which
   records). Do not run it.

STOP after the report.
```

Review focus: zero changes on correctly parsed records; the preview
counts; the re-ingestion procedure is reversible.

---

## Phase 6: ranking judgments and regression cases (H6, offline)

```text
Phase 6 (plan step H6): build the evidence for a ranking decision.
Offline and read-only against the application database. Do not change
production ranking (recipes/repository.py) in this phase.

Facts (verify):
- scripts/retrieval_eval/run_baseline.py writes to a hard-coded
  committed file, evals/results/phase1/baseline_fulltext_post_rebuild.json
  (a frozen historical result). Do not run it unmodified. Add an
  output-path option (default unchanged) or a wrapper, and write all
  outputs of this phase outside that file.
- Title-first ranking was tried on 2026-10-07 and reverted: it changed
  the top 5 of 14 of 52 frozen Phase 1 cases and moved 11
  judged-relevant hits out of the top 5, replaced by unjudged hits.
- The judging policy is in evals/results/phase1/README.md.

Required:
1. Reproduce the comparison (current ranking versus title-first)
   against the frozen Phase 1 cases, with the ranking change applied
   only inside the experiment (for example a parameter or a copied
   query), never in production code paths.
2. For the cases whose top 5 changed, list the newly surfaced unjudged
   hits and prepare judgments under the existing policy, with a short
   reason per judgment. Store them as a new, versioned judgment set
   with its own file and sha256; never edit the original labels or
   benchmark. Mark them as AI-prepared, pending owner review.
3. Add regression cases separating a dish request from an ingredient
   phrase: "adobo" (the dish) versus recipes containing "chipotle
   peppers in adobo sauce". Add at least two more pairs of the same
   kind if the corpus supports them, and say how you found them.
4. Compare three rankings: current, title-first, and a bounded title
   boost for explicit dish requests (for example setweight on the
   title within ts_rank, not unconditional precedence), keeping
   ingredient discovery separate. Report metrics against the original
   and the expanded judgments, plus the regression cases.
5. Write a recommendation for the owner with the risks. It is a
   proposal only.

STOP after the report.
```

Review focus: no frozen file touched (`git status` on evals/results);
judgments follow the policy and are labelled pending owner review; the
boost is bounded.

---

## Phase 7: harness coverage and decision logging (H7)

```text
Phase 7 (plan step H7): cover the new behaviour in the offline harness
and record observable decisions.

Facts (verify):
- evals/phase7_agent/cases.json is version phase7-cases-v8-2026-10-05
  (43 cases, sha256 2713af26...). A new version is made by editing
  cases.json with a new version string; run.py keeps the previous
  version's summary as history (see how history_v1 is kept) so the
  change stays auditable. Do not change the meaning of existing cases.
- evals/phase7_agent/verify_packet.py checks the Checkpoint C packet
  claims against raw files.

Required:
1. Create cases version v9 adding cases for: a technique answer after
   a plan keeps the phase; a dropped option is named in the feedback;
   a negated mention is not a pairing claim; the wrap-up withholds
   only the repeated tools; a repeated search is marked; the select
   phase withholds recipe search and pairings; a named allergy
   ("tree nuts") is checked; the allergy line keeps
   constraints_honored empty; plus phases 1 to 4 (swapped quantities,
   clipped directions, fidelity claims, budget-exhaustion finishing).
   Since H4, v8 case p7-budget-tools fails: its script has no turn
   after the excess batch, so the new finishing turn exhausts the
   script (internal_error). In v9, give it a scripted finishing turn
   (a question or a valid finish) with the matching expected result,
   and add a case that keeps the immediate tool-budget stop when the
   finishing turn is unaffordable (for example settings
   agent_output_token_ceiling 600). The v8 history must record that
   43/43 was the pre-H4 result.
   Keep the v8 results as history. Re-baseline results.json for v9
   only; this phase may change results.json. Update verify_packet.py
   so it still checks the Checkpoint C claims against their own
   version.
2. Record observable decisions in session_events: tools offered and
   withheld per turn with the reason, repeated results, validation
   failures, and remaining budgets. Never record raw model reasoning.
   Provider reasoning summaries, if available, may be stored only as
   optional diagnostics, labelled as such, and never used for
   decisions. Keep event payloads bounded.
3. Show these fields in scripts/sessions/export_session.py (JSON and
   timeline formats).
4. Demonstrate on an offline session (fake model, as the harness
   does) exported with the new fields.

STOP after the report.
```

Review focus: v8 history kept and auditable; each new case fails on
the pre-fix code where possible (say which); event sizes; no
reasoning text recorded.

---

## After phase 7 (not for the agent)

Checkpoint D: the owner reviews phases 1 to 7 and decides the H5
re-ingestion, the H6 ranking proposal, and freezing the build for H8
(commit SHA, full configuration, live budget). H8 is the live check on
the frozen build, run only with the owner's authorization.
