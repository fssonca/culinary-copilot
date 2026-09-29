# Owner decisions pending (Milestone 3)

Prepared 2026-09-29 with AI assistance. It lists the decisions only the
owner can make, with the context needed for each. Nothing here is decided
until the owner answers. The answers are recorded where each section says.

State at the time of writing:
- `main` is at `3d04da0` and in sync with GitHub.
- Phases 1–4 are done. Phase 5 (web search) is next.
- M3 live spend so far is about **$0.001** of the **$1.00** ceiling
  (technique embeddings about $0.001, vector-eval queries $0.00002).

| # | Decision | Blocks | Effort |
|---|---|---|---|
| 1 | Checkpoint A: judge the Phase 3 trajectories | Phase 3 live run | 15–20 min reading |
| 2 | Fix two loop gaps found while preparing this doc | Phase 3 live run | yes/no each |
| 3 | Phase 3 live run: budget, database, timing | Phase 3 live run | 3 choices |
| 4 | Phase 4 label spot-check | the Phase 4 eval record | 9 yes/no |
| 5 | Housekeeping | nothing | optional |

---

## 1. Checkpoint A: the Phase 3 trajectories

**What it is.** The plan's human checkpoint after the agent loop was
built. Read `evals/phase3_agent/REVIEW.md` (10 short trajectories), then
answer four questions.

**What you're looking at.** These are **scripted** runs. The model's
replies were written as test fixtures, so the packet shows how the loop
*handles* a model's behaviour: validation, stops, memory and labelling.
It does not show how a real model behaves. Real behaviour first appears in
the live run (decision 3).

### Q1. Does the agent ask sensible questions?
Two trajectories ask something:
- `yogurt-ask-resume`: "Do you have plain yogurt?" The user answers "no".
  The next run offers a coconut-milk adaptation.
- `empty-retrieval`: "I found no recipes for that. What dish should I
  look for?"

Consider whether these are the right moments to ask. For example, should
it ask about yogurt before searching, or only once a recipe needs it?

### Q2. Does it stop when it should?
Stop reasons across the packet:

| Stop reason | Cases |
|---|---|
| finished with enough evidence | 6 |
| needs user input | 2 |
| tool budget used up | 1 |
| wall-clock limit | 1 |
| no progress (same search 3×) | 1 |

Every limit stop returns an error with a next action ("start a new
session" or "retry"). Consider whether the messages are clear and whether
any of these stops should have happened earlier or later.

**Relevant to this question, see decision 2b.** Two runs stop with
"enough evidence" while skipping Epicure for a reason that doesn't fit the
request.

### Q3. Does it keep what you already answered?
- `yogurt-ask-resume`: the "no" to the yogurt question is kept (under
  "Confirmed answers kept") and drives run 2.
- `hard-constraint-conflict`: the vegetarian constraint survives. The
  model first proposed a chicken curry, the validator rejected it for
  "dropped hard constraint", and the retry offered only the lentil soup.

### Q4. Does it ever present an adaptation as if the source said it?
Every quantity line reads "from the source: …". The one adaptation reads
"adaptation (labelled adaptation): unverified substitution: coconut milk
for yogurt". Check that the difference would be obvious to a cook.

**Relevant to this question, see decision 2a.** Two runs present recipes
as "from the source" without any search in that session.

**How to answer:** one line per question, yes/no plus any comment. I'll
record the answers in `evals/phase3_agent/owner_review.json`, the same
way as the Phase 4 review of Milestone 2.

---

## 2. Two loop gaps found while preparing this doc

Both come from `evals/phase3_agent/trajectories.json`. The code was
checked.

### 2a. Options can cite recipes the agent never retrieved
In `hard-constraint-conflict` and `epicure-skip`, the session makes **no
tool calls**, yet the final options cite `lentil-2` and `curry-1` "from
the source".
- The validator (`agent/validate.py::validate_options`) checks that the
  recipe exists in the database and that the quantities match it. It does
  not check that this session actually retrieved it.
- Technique references already have the stricter rule: they must have
  been returned by a search in this session.
- The milestone criterion says recommendations "reference **retrieved**
  recipes".
- With a real model the risk is small, since it can't easily guess valid
  ids and quantities. But the check is cheap and matches the criterion.

**Decision:** require every option to come from a `search_recipes` or
`get_recipe` result in the same session, before the live run?
**Recommendation: yes.** It's a small fix with an offline test.

### 2b. Epicure skip reasons are accepted without checking they fit
The agent must consult Epicure by default, and may skip it only with a
reason from an allowlist. The loop checks that the reason is **on the
list**, not that it **fits the request**:
- "Vegetarian dinner, something hearty" was accepted with skip reason
  `direct_recipe_lookup`. That's an open request, not a lookup.
- "How do I boil an egg, and what soup goes with it?" was accepted as
  `simple_technique_question`. But its second half asks for a pairing,
  which is Epicure's job.

The criterion says the agent "records **justified** simple-task skips".
The learning plan goes further: "Query and evaluate Epicure by default,
**including for specific dish requests**" (`ai-learning-plan.md` line 42).
So `direct_recipe_lookup` as a skip reason contradicts the product policy.
The loop also allows a single-recipe answer only when Epicure was skipped
for that reason (`agent/validate.py:133`, `agent/loop.py:1476`). See
`docs/owner-decision-recommendations.md` §2b, which proposes removing that
skip reason and allowing a single recipe with Epicure still consulted.
A fully deterministic check is hard. Options:
- **(a)** Measure it in Phase 7 as the planned "Epicure compliance"
  metric, with these two as regression cases. No code change now.
- **(b)** Also add simple deterministic guards now. For example,
  `direct_recipe_lookup` requires the request to name a specific dish that
  a search found, and a skip is refused when the request contains a
  pairing cue ("goes with", "pair", "serve with").
- **(c)** Leave it; the live run will show whether a real model abuses
  skips.

**Recommendation: (a) plus a light version of (b)**, meaning the pairing
cue guard only. It's cheap and catches the second case. Deeper
justification checks can wait for Phase 7 data.

**How to answer:** "2a yes/no; 2b a/b/c". If any fix is chosen, I'll
write a short prompt for the implementing agent. It runs offline and
costs nothing.

---

## 3. Phase 3 live run

A small paid run of the real model through the agent loop. It's planned
in `evals/phase3_agent/LIVE_PLAN.md` and has not run yet.
`live_run.py` isn't written until you approve.

**What it would run:** 8 sessions, each retried at most once:
- normal flow;
- yogurt ask-and-resume;
- direct recipe request;
- vegetarian constraint conflict;
- empty retrieval;
- Epicure not configured;
- Epicure skip;
- no progress.

Budget and wall-clock stops are already covered offline.

### 3a. Budget
- Model `gpt-6-luna`: $0.10 per 1M input tokens, $0.50 per 1M output
  (recorded 2026-09-24; re-verify on the pricing page before the run).
- Each session is hard-limited to 30,000 input and 12,000 output tokens,
  so its worst case is **$0.009**.
- Bound: 8 sessions × 2 attempts × $0.009 = $0.144, rounded to
  **$0.15**. The run also stops itself when reported spend reaches $0.15.
- Realistic spend is far lower. The scripted sessions used about 11k
  input and under 400 output tokens, about **$0.001 per session**.
- **Decision:** approve $0.15, or set a different cap. Of the $1.00
  ceiling, about $0.85 would remain for Phase 5 (web search) and Phase 7
  (the final live evaluation).

### 3b. Which database the live sessions write to
- **Option A, the app database** (`culinary_copilot`). Sessions go into
  their own tables, which already exist since migration 005 was applied
  on 2026-09-28. Recipes and the technique corpus are only read. The live
  run adds about 8–16 session rows, next to the one smoke-test row.
- **Option B, a disposable copy.** Restore a backup into a `*_check_*`
  database and run there. The newest backup,
  `data/technique-migration-backup-006.sql`, was taken **before**
  006/007. It holds ledger 001–005 and the session tables, but no
  technique tables or embeddings. A copy would need 006/007 applied, the
  corpus loaded and the technique chunks re-embedded (about $0.001), on
  top of a 511 MB restore. (Corrected 2026-09-29: an earlier version of
  this line wrongly said the backup included 005–007.)
- **Recommendation: A.** The session tables were built for this and
  nothing else is written. `LIVE_PLAN.md` still says "needs the approved
  migration rehearsal first", which is out of date: 005 is applied.

### 3c. When
- **Now.** You see how the real model behaves before Phases 5–7 build on
  the loop. It costs about $0.01 realistically.
- **Folded into Phase 7.** One live campaign at the end. Cheaper to
  coordinate, but problems are found later.
- **Recommendation: now,** after the decision-2 fixes if you choose them.

**Also know:** `LIVE_PLAN.md` was written before Phase 4. The agent now
also has `search_techniques` and can cite technique pages, so the live run
would exercise that too. The token ceilings have enough headroom for the
extra tool definition. When `live_run.py` is written, the plan gets
updated to say this.

**How to answer:** "3a approve $0.15 (or $X); 3b A/B; 3c now/Phase 7".
Approving here does not start anything. The run itself still waits for
your explicit "run it" after `live_run.py` is built and tested with
fakes.

---

## 4. Phase 4 label spot-check

The technique retrieval eval (`evals/technique_retrieval/cases.json`) has
16 queries whose "right answer" pages were drafted by AI. The results so
far (full-text HitRate@5 0.812, vector 0.938) are only as good as these
labels. Please confirm the 9 riskiest.

For each row: is the labelled page where a good answer should come from?

| Case | Query | Labelled relevant page(s) | Note |
|---|---|---|---|
| tq-01 | sear chicken crust | Searing | |
| tq-02 | braise moist heat | Braising | |
| tq-04 | chicken internal temperature | FDA Safe Food Handling | holds the safe-temperature table (poultry 165 °F) |
| tq-05 | chill freeze storage | FDA Food Safety in Your Kitchen; Freezing (Wikibooks) | |
| tq-07 | sauté high heat | Sautéing | vector ranks Frying first |
| tq-11 | fix split curdled yogurt sauce | Sauce | **doubtful**: no page covers curdling since Emulsion was struck; the hit is only the word "sauce" |
| tq-12 | fluffy rice | Boiling Rice (Wikibooks); Cooked rice | |
| tq-15 | pink chicken inside | FDA Safe Food Handling | the page says colour is not a reliable sign; both modes miss it |
| tq-16 | browned pan sauce | Deglazing (Wikibooks); Sautéing | |

**Recommendation:** keep all of them except tq-11. Mark tq-11 as a
known coverage gap (no relevant page) rather than keep a label that
rewards a word match.

**How to answer:** "all fine", or list the case ids to change and what
to change. Corrections are recorded as owner decisions in the case file,
and the freeze hash is updated and disclosed. Both evals are then
re-scored at no cost for full-text and about $0.00002 for vector.

---

## 5. Housekeeping (optional, no deadline)

- **Two 511 MB backups in `data/`:**
  - `session-migration-backup-005.sql`, taken before 005;
  - `technique-migration-backup-006.sql`, taken before 006/007.

  Both are git-ignored. The 006 one supersedes the 005 one as a recovery
  point. Suggestion: delete the 005 backup once you're comfortable, and
  keep the 006 one until Milestone 3 is accepted.
- **Test databases** `culinary_test_embeddings` and
  `culinary_test_technique` in the local container are fixtures that the
  test suite recreates. Leave them.

---

## Coming later (no action now)

- **Phase 5, web search.** The agent verifies OpenAI's current
  `web_search` pricing and result format, builds everything offline with a
  fake search, then stops. You then approve the search price and a budget
  (checkpoint B), and review the logs and the "gap" queue.
- **Default technique search mode.** It stays full-text for now. Phase 7
  compares the agent's own choice against each fixed mode, with the
  chicken/"pink chicken" food-safety cases as regression tests.
- **Checkpoint C (end of milestone).** You read real live trajectories
  next to their sources and accept Milestone 3.

---

## Owner answers (2026-09-29)

In the owner's words: "Adopt the recommendations doc. For 3a, keep the
$0.15 cap with pre-call spending reservations. For 3b, use A only with
verified trial isolation; otherwise B. Keep both backups. Checkpoint A: I
agree with the suggested answers in the recommendations doc, including
their reservations. Proceed with the offline implementation and
fake-provider tests; the paid run waits for my explicit 'run it.'"

Resolved as follows. The recommendations are in
`docs/owner-decision-recommendations.md`.

1. **Checkpoint A:** the suggested answers were accepted, with their
   reservations. They are recorded in `evals/phase3_agent/owner_review.json`
   with findings P3-A-01 and P3-A-02, both open.
2. **2a:** yes. Require retrieval in the same session, with sufficient
   evidence.
   **2b:** remove the `direct_recipe_lookup` exemption, allow a single
   recipe while Epicure is still consulted, add a narrow pairing guard,
   check the degraded mode, and measure skip quality in Phase 7.
3. **3a:** a $0.15 cap, enforced with pre-call spending reservations.
   **3b:** A only with verified trial isolation, otherwise B.
   **3c:** after the fixes, before Phase 5.
   The paid run is **not authorized** until the owner says "run it".
4. **Labels:**
   - tq-05: replace `tech-fda-kitchen-33` with `tech-fda-safe-32`, and
     keep `tech-freeze-R6`.
   - tq-11: record it as a coverage gap with no relevant document.
   - The other seven spot-checked labels are kept.
   - The saved rankings are to be re-scored, with the coverage gap
     reported separately.
5. **Housekeeping:** keep both backups and the fixture databases.
