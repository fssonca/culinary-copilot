# Phase 7 owner decisions

Owner decisions for Milestone 3, Phase 7 (scenario evals and the bounded
live run). Each entry is recorded by an AI assistant and is not a
signature.

## Live run authorization (2026-10-04)

Recorded by an AI assistant; not a signature. The owner approved:

- **Run the Phase 7 live check** as prepared in
  `evals/phase7_agent/LIVE_PLAN.md`:
  - 7 scenarios from `evals/phase7_agent/live_scenarios.json`
    (sha256 `f202afb6…`);
  - one attempt each;
  - at most 1 web search in the whole run
    (`--search-max-per-live-session 1 --max-campaign-searches 1`);
  - hard run ceiling **$0.15**;
  - expected cost about $0.02;
  - run against the application database (`culinary_copilot` on
    `localhost`), as in the Phase 3 and Phase 5 live runs.
- **Phase 7 pool cap: $0.50** of the $1.00 Milestone 3 ceiling.
  Phase 3 used $0.1304 and Phase 5 $0.1015 before this run.
- **Commit Phase 7 part 1 first**, so the code that runs is recorded.

Known gap, accepted for this run: preflight checks for unacknowledged
search-estimate breaches only in the Phase 5 history, not in the
Phase 7 one. The in-run breach stop applies. Fix before any further
Phase 7 live run.

## Live run results (2026-10-04)

Run by an AI assistant at the owner's request, with the command from
`evals/phase7_agent/LIVE_PLAN.md` plus the environment
`HF_HUB_OFFLINE=1 LLM_RECOMMENDATION_ENABLED=true EPICURE_ENABLED=true`.
The first attempt was refused at preflight (`HF_HUB_OFFLINE` unset),
with nothing spent.

- **Spend:** $0.0292 of the $0.15 run ceiling, with 1 web search.
  Milestone 3 total is about $0.2611 of $1.00 (Phase 3 $0.1304,
  Phase 5 $0.1015, Phase 7 $0.0292).
- **Matched the expected stop (3 of 7):**
  - live-search-once: a cited web answer from provider evidence;
  - live-search-toggle: local options, no search;
  - live-yogurt-ask: options.
- **Did not match (4 of 7):**
  - **live-vegan-conflict.** The model asked for vector recipe search,
    which returned `tool_not_configured`; it then asked the user
    instead of retrying with full-text. The run was not
    representative. **Configuration error by the AI assistant:** every
    earlier live run set `EMBEDDINGS_ENABLED=true`, but this run did
    not (the Phase 7 plan's command listed no environment, and the
    assistant missed this flag when adding the others). This also
    shows a product defect: the search tool offers vector mode when it
    is not configured, and the error does not point back to full-text.
  - **live-chicken-e2e.** The note claimed the requested "30 minutes",
    which the time/temperature guard rejected as unsupported. The
    agent then asked whether to proceed with an option whose time is
    unconfirmed. This is honest behaviour, but the scenario had no
    scripted answer for it.
  - **live-peanut-allergy.** The agent asked about the allergy and
    recorded "peanuts". After resuming, it searched "chicken
    peanut-free dinner" (0 results) and asked again instead of
    retrieving and checking recipes.
  - **live-plan-safety.** Options were reached, the user selected one,
    and the plan was correctly rejected for a missing food-safety
    reference. The session then ran out of steps (`agent_max_steps`)
    before a corrected plan. This is live evidence for P3-L-12: the
    session budget is too tight for recommend plus plan.
- **Checks seen working live:**
  - the time-claim guard;
  - the raw-poultry food-safety requirement on plans;
  - Epicure consulted;
  - the search toggle;
  - the provider-evidence citations;
  - the run ceiling and the 1-search limit.
- **Possible false positives to investigate:** the note-pairing check
  flagged "curry" (a dish name the user asked for) and "curry powder".
- Raw trajectories are in `data/phase7-live/raw/` (gitignored); the
  summary is in `data/phase7-live/live-summary-phase7.json`.

## Owner answers after the live run (2026-10-04)

Recorded by an AI assistant; not a signature.

- **The session step budget is raised from 8 to 12**
  (`SESSION_MAX_STEPS`, P3-L-12). Tool calls stay at 12. Applied in
  `config.py`, `domain/sessions.py` and `.env.example`, with the tests
  updated.
- **A live re-run of the 4 unmatched scenarios is authorized**
  (live-chicken-e2e, live-peanut-allergy, live-vegan-conflict,
  live-plan-safety):
  - embeddings on, no web searches;
  - ceiling **$0.10**, expected about $0.015;
  - run after the budget change is verified.
- Found by the assistant after the answer: the per-session input-token
  ceiling (`AGENT_INPUT_TOKEN_CEILING`, 30k) binds before 12 steps.
  The live sessions used about 3k-4.5k input tokens per turn, and
  plan-safety used 24.7k in 7 turns. So the step increase alone does
  not give the plan room. This was put back to the owner as a separate
  question.
- **Owner answer: the input-token ceiling is raised from 30k to 60k**
  (`AGENT_INPUT_TOKEN_CEILING`). The output ceiling stays 12k. The
  worst case per session is about $0.012 in model tokens. Applied in
  `config.py` and `.env.example`, with the docs updated. The loop test
  calibrated to 30k now pins 30k explicitly.

## Live re-run results (2026-10-04)

Run by an AI assistant as authorized above:
- the same command shape with `EMBEDDINGS_ENABLED=true`;
- the 4 unmatched scenarios, with the new budgets (12 steps, 60k
  input) and the fixes from the offline round;
- the working tree on top of commit `d4913b0`.

The runner's `--ceiling-usd` is cumulative for the Phase 7 pool, so
this run could spend at most $0.0708 ($0.10 − $0.0292).

- **Spend:** $0.0184, no web searches. The Phase 7 pool now stands at
  $0.0476. Milestone 3 total is about $0.2795 of $1.00.
- **Now matched (2 of 4):**
  - **live-vegan-conflict:** options, constraints honored.
  - **live-plan-safety:** options; the user selected one; the plan was
    rejected for the missing food-safety reference; the agent looked up
    safe temperatures; an accepted plan followed. This is the
    recommend-select-plan flow working end to end under the new budget.
- **Still unmatched (2 of 4), both stopped safely on the tool-call
  budget (12), with nothing unsafe offered:**
  - **live-chicken-e2e.** The note kept calling options "30-minute".
    The time guard rejected it twice, and the model never dropped the
    claim. One recipe was fetched with a malformed id. The time guard
    reads ingredient lines and directions, not titles, so a recipe
    title stating a time does not count as evidence.
  - **live-peanut-allergy.** The agent asked about the allergy first
    (improved), and the answer was recorded. Its finish then claimed a
    `dietary_constraints` key that is not a session constraint, and was
    rejected. It then re-fetched recipes it already had until the tool
    calls ran out.
- **Checkpoint C signals so far:**
  - no constraint was relaxed in any live run;
  - unsupported claims (time, pairing, missing safety reference) were
    caught by the guards before the user saw them;
  - the web citations came from provider evidence.

  The remaining failures are efficiency and recovery problems, not
  safety problems.
- Raw trajectories are in `data/phase7-live/raw-rerun/` (gitignored);
  the summary is in `data/phase7-live/live-summary-phase7-rerun.json`.

## Checkpoint C review (2026-10-05)

Recorded by an AI assistant; not a signature.

The owner reviewed the packet (`evals/phase7_agent/CHECKPOINT_C.md`),
the saved trajectories and the cited pages. Their answers:

- **Were any claims unsupported? Yes.**
  - The accepted curry plan (odunola/foodie foodie-013643) said the
    recipe has no directions, but the stored record has six. The model
    had never been shown directions. The server still labelled the
    plan steps "source".
  - The accepted web answer described a cooking method, which goes
    beyond the discovery-only boundary.
  - The owner found that all 24 quantity entries in the two yogurt
    options match the stored recipes.
  - The 165°F poultry instruction is supported by the cited FDA chunk.
- **Were any constraints relaxed?** No explicit constraint relaxation
  was observed in the reviewed live runs.
  - Allergy-aware recommendation after resuming is unproven: the
    peanut sessions stopped without options.
  - Allergen checks cover listed-ingredient evidence only and cannot
    establish the absence of cross-contact.
- **Were the search citations real and relevant? Mostly yes.**
  - All three URLs are recorded in provider evidence.
  - The JETRO and Just One Cookbook pages were checked and are
    relevant.
  - The PBS page returned 403, so it was not fully reviewed.

**Correction to the re-run section above.** It said unsupported
claims never reached the user and that the remaining failures were
not safety problems. Both statements are wrong. The supported
statement is: the observed failures include recovery and efficiency
problems; broader safety conclusions remain unproven.

**Decisions:**

- **Status of the Phase 7 live results:** diagnostic evidence, not
  acceptance.
- **Checkpoint C:** kept open for an offline correction pass, with no
  further paid run needed to establish the defects. The pass changed
  four things:
  - the model now sees recipe directions;
  - the plan is labelled "source" only when the steps cite and cover
    every stored direction;
  - the web answer guard enforces the discovery-only boundary;
  - the evaluation record was corrected.

  An AI assistant verified it on 2026-10-05: `make check` 1172
  passed, 8 skipped; harness `phase7-cases-v8-2026-10-05` 43/43,
  4/4 adversarial; `verify_packet.py` all claims matched.
- **Budgets:** unchanged at 12 steps, 12 tool calls and 60k input
  tokens.
- **Paid runs:** none further authorized.
- **Carried to Milestone 4,** each with an acceptance criterion in the
  packet (the recovery fixes are offline only and not live-evaluated):
  - live ask-and-resume to checked options;
  - live allergy-aware recommendation after resuming;
  - live confirmation of plan attribution and of the web guard;
  - the technique-mode comparison;
  - whether a bounded post-answer allowance is needed.
- **Milestone 3 acceptance:** pending the owner's explicit decision.

## Demo hardening (2026-10-06)

Recorded by an AI assistant; not a signature. These changes came from
live demo sessions the owner ran with `make demo`; the owner asked for
fixes to be made directly. None is live-confirmed yet.

- **Plan attribution admission:** the checkpoint C label rule is
  unchanged (a plan is "source" only when every step is supported and
  every direction is cited). What changed is the older Phase 3
  requirement (P3-L-09) that the model write an admission for a
  `model_adaptation` plan. When the selected source has directions,
  the app now writes that adaptation note itself, naming the steps
  and the words that differ, instead of rejecting the plan. Three
  live plans had failed only on the missing admission.
  Ingredient-only sources still need the model's own admission.
  Known implications: no pressure toward faithful plans (expect more
  `model_adaptation` labels); the model's own note is not checked for
  claims of fidelity; no harness case covers the new path yet.
  **Owner decision (2026-10-06): keep as is for now.**
- **Recovery and retrieval:** technique search matches "poultry" for
  chicken, turkey, duck and goose; technique excerpts reach the model
  at 600 characters; a repeat-only step earns one tool-less wrap-up
  turn; the repeated-call stop now charges its step; the duplicate
  recipe pointer names the earlier output.
- **Checks:** `make check` 1192 passed, 8 skipped; harness
  `phase7-cases-v8-2026-10-05` 43/43, 4/4 adversarial, results
  unchanged.

## Demo hardening follow-up (2026-10-07)

Recorded by an AI assistant; not a signature. From the owner's
2026-10-07 adobo session, whose technique search found and cited the
right FDA chunk but whose plan phase took 6 model turns and lost
source content. The owner asked for the fixes. None is live-confirmed
yet.

- **Directions reach the model in full:** `get_recipe` showed each
  direction cut at 200 characters, and `directions_truncated` stayed
  false because it only counts dropped directions. The plan lost a
  simmer time, two ingredients and the source's own thermometer check,
  and the model said those were absent from the source. Directions now
  reach the model at up to 600 characters (12 directions), and a
  direction cut there is listed in `directions_clipped`. 64% of
  corpus recipes had a direction over 200 characters; 184 of 16,033
  now have one over 600. The largest summary measured over the corpus
  is 3,926 characters.
  - *Correction (2026-10-07, owner review):* the heading above
    overstates this. Directions are expanded (up to 12 directions of up
    to 600 characters each) with explicit clipping, not sent in full:
    184 recipes still have a clipped direction and 421 have more than
    12 directions. Access to the clipped content is planned as step H3
    of `docs/milestone-3-hardening-plan.md`.
- **Food-safety requirement stated up front:** once a dish is
  selected and its source has raw meat, poultry, fish or eggs, the
  turn input says the plan needs a food-safety `technique_ref`, and
  names the safety chunks once returned. The validator is unchanged;
  this removes the rejected-plan turn.
- **Repeat-fetch pointers count as repeats:** full fetch, then
  pointer, then pointer used to look like two different results. The
  wrap-up came a step late, and the stall stop could never fire.
- **Attribution matching:** accents are folded ("jalapeño" matches
  "jalapeno"); bare citation tags such as "[Source direction 2]" in
  step text are ignored (a tag with other words still counts); the
  note no longer calls directions "not cited" when they are cited by
  ungrounded steps. The framing asks for no tags in step text and for
  added safety checks as their own adaptation.
- **Checks:** `make check` 1198 passed, 8 skipped; harness
  `phase7-cases-v8-2026-10-05` 43/43, 4/4 adversarial, results
  unchanged; `verify_packet.py` all claims match.

### Second 2026-10-07 session

Recorded by an AI assistant; not a signature. From the owner's second
adobo session: the follow-up fixes above worked (safety search in the
first plan step, all five directions in the plan, no rejection), and
the session exposed the items below. None is live-confirmed yet.

- **Wrong amount in plan text:** the mise en place said "1 1/2 lb" for
  a source amount of 5 1/2 pounds, stored as the exact fraction
  "11/2". The model now sees the source's own notation ("5 1/2") when
  it parses to the same value; the UI shows improper fractions as
  mixed numbers; and a validator rejects mass or volume amounts in the
  mise en place, steps or plating that the source never states with
  that unit. Checked against the corpus: a plan made of each recipe's
  own ingredient lines and directions passes for all 16,033 recipes.
  - *Correction (2026-10-07, owner review):* this is a limited
    amount/unit check. Each amount is compared with every amount the
    recipe states, without the ingredient it belongs to, so a swapped
    amount passes when another ingredient has it. Ingredient-aware
    validation is planned as step H2.
- **Unrequested scaling:** the model scaled a source without servings
  and retried the refusal. The framing now says to scale only when
  the user asks for a number of servings, and `scale_recipe` is not
  offered when the selected source lists no servings.
- **History cap:** older whole turn groups now stay while the history
  is within 16,000 characters (at most 31 items), beyond the newest
  13 items.
  - *Correction (2026-10-07, owner review):* the 16,000-character
    limit is a retention heuristic for keeping small outputs in view,
    not a token or spending guarantee; the input-token ceiling and the
    budgets are the spending limits.
- **Title-first recipe ranking: tried and reverted, owner decision
  open.** "adobo" ranked ten "chipotle peppers in adobo sauce" recipes
  above all 14 adobo dishes. Ranking full title matches first fixed
  that, but on the frozen Phase 1 cases (read-only, results written
  outside the repository) it changed the top 5 of 14 of 52 cases and
  moved 11 judged-relevant hits out of the top 5 (grade 2: 42 to 36,
  grade 1: 11 to 6), replaced by unjudged hits. The same ranking
  serves the HTTP search and the recommendation pipeline, so it was
  not kept. The agent recovered in this session with one vector
  search.
- **Checks:** `make check` 1202 passed, 8 skipped; harness
  `phase7-cases-v8-2026-10-05` 43/43, 4/4 adversarial, results
  unchanged; `verify_packet.py` all claims match.

## Live evaluation, five sessions (2026-10-07)

Recorded by an AI assistant; not a signature. The owner authorized
five live sessions (request, then answers or a choice, then a
technique question). They ran against a separate local server with
the `make demo` limits (40 steps, 40 tool calls, 300k/60k tokens,
240 s per run), driven through the same HTTP API as the UI. Fixes were
made between runs, so later runs in a session used newer code.
About 736k input and 14k output tokens in total.

| Session | Request | Outcome |
|---|---|---|
| `ses-395e87afd7d2` | adobo | options, plan (`source`), technique answer after a phase fix |
| `ses-61347af29b8d` | lentil dinner, vegetarian | one option after three failed runs, plan, technique answer |
| `ses-983446426ab7` | baking for a party | three questions, two stall stops, tool budget exhausted |
| `ses-fc99a4ef228c` | baking for a party (rerun) | options, plan, technique answer |
| `ses-0b0a3c7d4ea2` | dessert, friend's allergy | question, two stall stops, tool budget exhausted |

**Fixed (tests added; harness unchanged):**

- A technique or web answer after a plan always failed ("plan ->
  recommend" is not allowed); answers now keep the dish phase.
- The answer "Tree nuts" mapped to no allergen, so options were never
  checked for tree nuts; generic names now map, and a generic "nut"
  ingredient line now violates tree nuts (unverified for peanut).
- An option dropped for a dietary violation was silent, so a note that
  described it failed with no explanation and the run stalled; the
  feedback now names the drop.
- Note and answer checks treated negations ("contains no meat") and the
  user's own ingredient ("lentils") as unsupported pairings.
- The framing forbade re-fetching a recipe fetched in an earlier run,
  which is not in the new run's history, so the model asked the user
  instead; re-fetching across runs is now allowed.
- A tool-less wrap-up after one repeated search left one fetched
  recipe, so the model could only ask questions; the wrap-up now
  withholds only the repeated tools, and repeated searches say they
  returned nothing new.
- After "Choose this" the model redid discovery; recipe search and
  pairings are not offered until the plan exists.
- The food-safety requirement line kept appearing after the plan and
  re-triggered the safety search; the plan's steps now reach follow-up
  questions; the framing no longer invites `dietary_constraints` in
  `constraints_honored` when the session has none.

**Not fixed, for the owner:**

- **Data:** 158 `odunola/foodie` records are titled "summary", with
  their ingredients and directions collapsed into one line each (an
  ingestion parsing bug); they appear in search results. Fixing them
  needs a re-ingestion of the application database.
- **Budgets:** failed runs spend the session's 40 tool calls, so two
  sessions ended with "start a new session". A batch larger than the
  remaining calls stops the run at once (documented behaviour) instead
  of giving a final tool-less turn.
- **Unconfirmed:** the party-baking and allergy scenarios did not
  complete; the fixes made for them are tested offline only and need
  live runs to confirm.
- **Technique coverage:** the corpus has little on crisping skin; full
  text found only weak partial matches and the useful chunk came from
  one vector search.
- **Observability:** the trajectory does not record the model's
  reasoning, so why the model kept searching instead of finishing is
  inferred, not observed.

## Owner review of the hardening work (2026-10-07)

Recorded by an AI assistant; not a signature. The owner reviewed the
demo hardening, its follow-ups and the five-session live evaluation,
and asked for a plan, now in `docs/milestone-3-hardening-plan.md`.

- **Kept:** the hardening changes (expanded directions with explicit
  clipping, the up-front safety requirement, accent and citation-tag
  normalization, the app-written adaptation note, scaling refused
  without servings, history retention). Global title-first ranking
  stays reverted.
- **Corrections to earlier wording here:** directions are not "in
  full" (12 directions of up to 600 characters; 184 recipes still have
  a clipped direction and 421 have more than 12); the prose quantity
  check is a limited amount/unit check, since it compares amounts with
  every amount in the recipe without their ingredient (a swapped
  "1 1/2 lb chicken" for a 5 1/2 lb source passes when another
  ingredient is 1 1/2 lb); the instruction that party size "never"
  matters is too absolute; the 16,000-character history setting is a
  retention heuristic, not a spending guarantee.
- **Priorities:** ingredient-aware quantity validation; access to
  clipped directions; an offline repair preview for the 158 malformed
  records; budget-exhaustion recovery without raising limits; expanded
  ranking judgments and dish-versus-ingredient regression cases; harness
  coverage and decision logging before any frozen-build live check.
- **Still needs explicit authorization:** the baseline commit (H0),
  any application-database re-ingestion, and the live check (H8).

## Checkpoint D (2026-10-08)

Recorded by an AI assistant; not a signature. The owner answered the
decisions in `docs/checkpoint-d-decisions.md`, adopting the
recommendations in `docs/checkpoint-d-recommendations.md`.

1. **H6 judgments.** The 18 proposed grades stand under the existing
   conventions (row 3 grade 1, the rest grade 2), with corrected
   explanations. The 5 recipes only the title boost surfaces are grade
   2, with suitability recorded separately: Pasta e Fagioli (DEV-24) is
   not suitable (vegan violated by chicken broth; gluten-free
   unresolved). Stored as `evals/results/h6/additional_judgments_v2.json`.
2. **Ranking.** Option A: keep the current ranking. A title boost needs
   further evaluation after H8: explicit search intent, complete pooled
   judgments on development cases, a weight chosen on development cases
   and confirmed on fresh held-out cases. The H6 metric first called
   Recall@5 is Hit@5; the outputs and README say so.
3. **Malformed recipes (H5).** The scoped removal and quarantine is
   approved once the procedure is corrected and rehearsed: every
   rehearsal write bound to an explicitly named disposable database
   that refuses the application target; a frozen identity manifest;
   assertions that abort the transaction; explicit first-run and rerun
   states with the same import id; quarantine and deletion atomic, or
   the intermediate state documented and tested. Keep the 38 older
   quarantine rows as import history (report unique rows separately
   from quarantine events). Title recovery is deferred.
4. **H8.** Demo limits, a frozen build on the cleaned corpus, two fresh
   sessions (party baking and allergy), web search off for both, and
   **$0.15 total enforced across both sessions** by a shared ledger
   that reserves before each paid call and reconciles reported usage.
   `make demo` alone does not enforce a dollar ceiling. Freeze the
   final reviewed commit, corpus counts and import id, model, reasoning
   effort, service tier, all limits, retrieval/Epicure/embedding
   settings and the scenario scripts. Predeclare success per workflow;
   report first attempts separately; a code change means a new freeze.
   Reconcile the unrecorded 2026-10-06/07 usage with provider billing
   first. Running H8 still needs the owner's go-ahead. Two passing
   sessions demonstrate the workflows under demo limits; they do not
   establish general reliability or Milestone 3 acceptance.
5. **Push.** Push the reviewed branch when authorized; decide on
   merging into `main` after reviewing H8.

### Go-ahead for the H5 application run and H8 (2026-10-08)

Recorded by an AI assistant; not a signature. The owner told the
assistant to proceed with the actions awaiting go-ahead, with a budget of
$1 for live calls and no push.

- **H5:** applied to the application database after a fresh backup and a
  rehearsal on that backup (`docs/h5-summary-cleanup.md`, "Application
  run record").
- **Billing:** the 2026-10-06/07 usage was not reconciled against
  provider billing; the assistant has no access to billing. The owner's
  $1 for this session is recorded as the budget the $0.15 H8 pool must
  fit, which it does. The reconciliation remains open for the owner.
- **H8:** run with acknowledgement `h8-checkpoint-d-2026-10-08` under the
  freeze recorded in `evals/h8_live/PLAN.md`.
- **Push:** not authorized; the branch stays local.

H8 result (recorded by an AI assistant): 0 of 2 sessions complete on
the first attempt, both stopped by the repeat-stall guard; $0.0143
spent of the $0.15 pool. See `evals/h8_live/RESULTS.md`, which also
records two grading bugs found and fixed and proposes the next step for
the owner to decide.

### After the first H8 attempt (2026-10-08)

Recorded by an AI assistant; not a signature. The owner agreed with the
recommendations in `evals/h8_live/RESULTS.md`: a stall now gets one
tool-less finishing turn (`91c5a71`), and H8 runs again as a separately
labelled second attempt on fresh scenarios (`scenarios_v3.json`). The
owner set the H8 pool to **$1.00** (attempt 1's $0.0143 counts against
it). Push still not authorized.

H8 attempt 2 result (recorded by an AI assistant): 0 of 2 complete, both
further than attempt 1; the stall recovery worked in both sessions. One
validator false positive was fixed afterwards; the follow-up-as-plan
behaviour and a third attempt await the owner. See
`evals/h8_live/RESULTS.md`, "Second attempt".

### After the second H8 attempt (2026-10-08)

Recorded by an AI assistant; not a signature. The owner approved the
recommendations: the plan-phase follow-up framing (option b; option a,
rejecting a re-issued plan, only if attempt 3 still shows the problem)
and a third, separately labelled attempt on fresh scenarios
(`scenarios_v4.json`). Push still not authorized.

H8 attempt 3 result (recorded by an AI assistant): 1 of 2 complete (the
allergy sleepover session); the office-birthday plan was rejected by a
quantity-check false positive, fixed afterwards. H8 total $0.0885 of
$1.00. See `evals/h8_live/RESULTS.md`, "Third attempt".

### After the third H8 attempt (2026-10-08)

Recorded by an AI assistant; not a signature. The owner approved the
recommendations: the selected recipe goes into the plan run's input
(instead of withholding the full fetch), and a fourth, separately
labelled attempt runs on fresh scenarios (`scenarios_v5.json`). Push
still not authorized.

H8 attempt 4 result (recorded by an AI assistant): 1 of 2 complete (the
family picnic session); the potluck plan was rejected by an
equipment-size false positive (fixed afterwards), then by the fidelity
check over an author-credit "direction" in the source (open). H8 total
$0.1133 of $1.00. See `evals/h8_live/RESULTS.md`, "Fourth attempt".

## Milestone 3 closed; new goal (2026-10-08)

Recorded by an AI assistant; not a signature. The owner closed
Milestone 3 as the grounded baseline (close-out in
`docs/milestone-3-hardening-plan.md`) and set a new goal: stored recipes
as references and guidance rather than the answer itself, with Epicure
and retrieval supporting adaptation and composition under explicit
provenance. Plan: `docs/milestone-4-plan.md` (drafted, not approved).
No measurement batch was run. Push still not authorized.
