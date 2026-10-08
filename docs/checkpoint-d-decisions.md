# Checkpoint D: decisions for the owner

Prepared 2026-10-08 with AI assistance. It lists the decisions that close
the Milestone 3 hardening plan (`docs/milestone-3-hardening-plan.md`) and
the context for each. Nothing here is decided until the owner answers.
Record the answers in `docs/phase7-owner-decisions.md` under a new
"Checkpoint D" section, as with the earlier checkpoints.

## State at the time of writing

- Branch `m3-checkpoint-c-demo-hardening` at `5fd5300`, 10 commits ahead
  of GitHub, none pushed. Every phase H0 to H7 is committed. Each commit
  was implemented by an AI coding agent and reviewed with AI assistance.
- Checks on `5fd5300`: `make check` 1,271 passed, 8 skipped; offline
  harness cases v10, 55/55, 4/4 adversarial cases caught;
  `verify_packet.py` matches all Checkpoint C claims.
- The application database is unchanged by the hardening work. It still
  holds 16,033 recipes, including the 158 malformed `summary` records.
- Model settings for the agent: `LLM_REC_MODEL` (`gpt-6-luna` in
  `.env.example`) and `LLM_REC_REASONING_EFFORT` (default `none`). List
  prices: $0.10 per million input tokens ($0.01 cached), $0.50 per
  million output tokens.

## Summary

| # | Decision | Depends on | Effort |
|---|---|---|---|
| 1 | Review the 18 AI-prepared ranking judgments and judge 5 more | — | 20–30 min with the review sheet |
| 2 | Ranking: keep the current ranking, or plan a title boost | 1 | one choice, two sub-questions if "boost" |
| 3 | Re-ingest the 158 malformed recipes | — | yes/no, two sub-questions |
| 4 | Freeze the build for the H8 live check: commit, configuration, budget | 2, 3 | four choices |
| 5 | Push the 10 local commits | — | yes/no |

Suggested order: 1, 2, 3, then 4. Decision 5 can happen at any time.

---

## 1. Review the H6 ranking judgments

**What it is.** H6 tested whether ranking recipes whose title matches the
request higher would help. The comparison surfaced recipes that the
original Phase 1 labels never judged, so an AI agent judged 18 of them
under the existing rubric (`evals/rubric_v1.md`). They are stored as a
separate layer, `evals/results/h6/additional_judgments_v1.json` (sha256
`2e79ee34…`); the original labels are untouched.

**Why it matters.** Any case for changing the ranking rests on these
labels (see decision 2). Without them, both ranking changes score below
the current ranking.

**What to look at.** `docs/checkpoint-d-h6-judgment-review.md` lists all
18 with the request, the recipe, the grade and the reason, plus 5
recipes that only the title boost surfaces and nobody has judged yet.
Points worth a closer look:

- 17 of the 18 are grade 2 ("same dish"). The rubric reserves 2 for the
  requested dish or a named variant.
- Single-ingredient requests ("garlic") follow a recorded precedent:
  in case DEV-15 the original labels gave grade 2 to dishes named after
  garlic. In DEV-18 and DEV-19 the original labels only had recipes
  where garlic is one ingredient, graded 1.
- 7 of the 18 are on held-out cases (HELD-09, HELD-14, HELD-17). Held-out
  cases exist to test a choice made on the development cases; labelling
  them with the same AI process and then using them to choose a ranking
  weakens that separation.
- Topical grades ignore constraints by rule. One unjudged boost recipe
  (DEV-24, "Pasta e Fagioli") answers a vegan and gluten-free pasta
  request, so its suitability check matters.

**Your answer.** For each row: agree, or the grade you would give. Grade
the 5 unjudged recipes. The answers become version 2 of the layer; the
comparison is then re-run.

---

## 2. Ranking: keep the current ranking or plan a title boost

**What it is.** On 2026-10-07 a "title-first" ranking (recipes whose
title matches come first) fixed the "adobo" request but was reverted
because it pushed judged-relevant recipes out of the top 5 in 14 of 52
frozen cases. H6 reproduced that and tested a bounded alternative: the
current score plus 2.0 times the title's own score, so a title match
raises a recipe without overriding everything else.

**Evidence.** Full report: `evals/results/h6/README.md`. On the same 23
cases (those where the current top 5 holds a judged recipe; a top 5
with no judged recipe counts as 0):

| Ranking | Recall@5, grade 2, recorded labels | MRR@5, grade 2, recorded labels | Recall@5, grade 2, with the 18 new judgments | MRR@5, grade 2, with the 18 new judgments |
|---|---|---|---|---|
| current | 0.783 | 0.523 | 0.783 | 0.523 |
| title-first | 0.696 | 0.462 | 0.913 | 0.546 |
| title boost | 0.696 | 0.491 | 0.913 | 0.551 |

Under both experiments, 3 cases (DEV-18, DEV-19, HELD-17) lose every
judged recipe from the top 5. The boost changes the top 5 of 16 cases;
title-first changes 14. On the dish-versus-ingredient regression cases
the boost puts 4 adobo dishes in the top 5 (current: 0), 3 tzatziki
dishes (current: 1), and moves hollandaise sauce from rank 4 to rank 2.
The three ingredient-phrase controls keep the same top 5.

**Options.**

- **A. Keep the current ranking (recommended for now).** Nothing
  changes. Adobo-style requests stay weak.
- **B. Plan the title boost.** Only after decision 1 confirms most of
  the 18 labels. It is then implemented in `recipes/repository.py`
  with harness and regression coverage, and the comparison is re-run
  before the build is frozen. Two sub-questions:
  - **Free text.** The agent's searches are free text, and free text
    carries no signal that the user asked for a dish. As tested, the
    boost would apply to every agent search. Accept that, or require a
    dish signal first (more work)?
  - **Weight.** 2.0 is the smallest of 0.5, 1.0, 1.5, 2.0 and 4.0 that
    fixes adobo, and adobo is also one of the regression cases, so the
    weight was tuned on its own test. Accept 2.0, or ask for a weight
    chosen on more cases?
- **C. Title-first.** Not recommended: same losses as the boost, lower
  ranking quality, and it also reorders ingredient-discovery requests.

**Recommendation.** A now. Revisit B after decision 1, with a dish signal
and a weight chosen on more than one case.

---

## 3. Re-ingest the 158 malformed recipes

**What it is.** 158 `odunola/foodie` recipes are titled "summary" and
appear in search results (for example, rank 2 for "hollandaise"). H5
found the cause: a block of 218 source rows (CSV rows 19349–19566) has
the word "summary" where the title belongs, and the ingredients and the
steps each collapsed into one line. None of the 218 rows carries a dish
title, so they cannot be repaired automatically, and paid extraction
cannot invent a title either. Since H5 the parser refuses this layout
and routes it to quarantine, but the application database still holds
the 158 recipes loaded before.

**What the re-ingestion would do** (proposed procedure:
`data/h5-repair-preview/REINGESTION-PROCEDURE.md`, local only, not
committed):

- delete the 158 recipes (their 158 embedding rows go with them);
- record 201 rows in quarantine with the reason
  `summary_layout_missing_title` (the 158, 38 rows already quarantined
  by the earlier migration, and 5 exact duplicates);
- leave 17 rows alone: they were never imported (outside the
  migration's scope);
- expected end state: recipes 16,033 → 15,875; quarantine rows
  443 → 644; foodie embedding rows 14,817 → 14,659;
- cost: no model calls, no new embeddings.

**Safeguards required before it runs.** A fresh backup restored and
verified in a separate database; the full sequence rehearsed twice on a
disposable database; one transaction for the delete, with every count
checked inside it and a rollback on any mismatch. The delete script in
the proposal is a sketch: it must be written out and reviewed first,
because the existing load command never deletes.

**Sub-questions.**

- **The 38 already-quarantined rows** would get a second quarantine row
  under the new import id, so each appears twice (old reason and new).
  That is how the 443 → 644 count arises. Accept the duplicates as
  audit history, or supersede the old rows?
- **Titles for the 218 rows.** A few descriptions name the dish; most do
  not. Recovering them would need someone to review the 218 by hand.
  Pursue that later, or leave them quarantined?

**Side effect already in the code.** The parser and routing version bump
in H5 invalidates the cached model extractions for every foodie record,
not just these 218. This re-ingestion needs no extraction, but any
future full re-preparation of the foodie data would pay to re-extract.

**Options.** Authorize the re-ingestion with the safeguards above, or
leave the database as it is (the 158 keep appearing in searches).

**Recommendation.** Authorize it, before the H8 live check, so the live
check sees the cleaned corpus. Accept the duplicate quarantine rows.
Leave title recovery for later.

---

## 4. Freeze the build for the H8 live check

**What it is.** H8 is the only check of the hardening work against the
real model. Two live sessions on a frozen commit and configuration, with
no code changes between them:

- **party baking:** a vague request, options, a selection, a plan, then
  a technique question;
- **allergy:** an unnamed allergy, the agent's question, the named
  answer, checked options, a plan, then a technique question.

Both failed on 2026-10-07 (stalls, then the tool budget ran out). The
fixes since then are tested offline only. Each session's report covers
outcome, steps, tool calls, tokens, cost, validation failures and any
claim not supported by its sources.

**Choices.**

1. **Commit to freeze.** `5fd5300` plus this document if decisions 2
   and 3 change no code. If a ranking change is planned (2B), freeze
   after it is implemented and re-checked. If decision 3 is approved,
   record the database counts after the re-ingestion with the freeze.
2. **Limits.** The `make demo` limits used on 2026-10-07 (40 steps, 40
   tool calls, 300k input and 60k output tokens per session, 240 s per
   run), or the ordinary limits (12 steps, 12 tool calls, 60k and 12k
   tokens, 90 s). The demo limits make the result comparable with
   2026-10-07; the ordinary limits are what a normal session gets. No
   limit is raised in either case.
3. **Other settings.** As in `make demo`: Epicure on, embedding calls on
   (query embeddings for vector search, a small extra cost), full-text
   retrieval by default, trajectory recording on, the operator web
   search switch on with the per-session web toggle left off. Model and
   reasoning effort as set in `.env` when frozen; both are recorded with
   the freeze. With reasoning effort `none` the provider returns no
   reasoning summaries, so the H7 diagnostic stays empty.
4. **Budget.** Worst case at the demo limits is about $0.06 per session
   at list prices without caching (300k input tokens cost $0.03, 60k
   output tokens $0.03), so about $0.12 for both. At the ordinary limits
   it is about $0.012 per session. For comparison, the five sessions on
   2026-10-07 used about 736k input and 14k output tokens, about $0.08
   (my estimate; not recorded at the time). The last recorded Milestone 3
   total is about $0.28 of $1.00 (2026-10-04). The 2026-10-06 and
   2026-10-07 live sessions are not in that total, so it should be
   checked against the provider's billing before approving.

**Recommendation.** Demo limits, a hard ceiling of $0.15 for both
sessions, web search off for the sessions, run by you or by an
assistant on your explicit go-ahead. The 2026-10-07 sessions stay
diagnostic evidence, not confirmation.

---

## 5. Push the local commits

**What it is.** The 10 hardening commits (`eae6f00` to `5fd5300`) exist
only on this machine. Pushing the branch makes them visible on GitHub; it
does not merge into `main` or deploy anything.

**Recommendation.** Push the branch once you have read this document.
Merge into `main` after the H8 live check.

---

## Not part of this checkpoint

Open items from the 2026-10-07 evaluation that no phase addressed:

- The technique corpus has little on crisping skin; full-text search
  found only weak matches.
- Failed runs still spend the session's tool calls. H4 adds a finishing
  turn after a batch that exceeds the budget, but it does not refund
  calls spent by failed runs.
