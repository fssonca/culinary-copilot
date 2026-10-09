# Milestone 4 evaluation design (R2)

Proposed 2026-10-09 for Checkpoint E, with
`docs/adr/0003-references-not-answers.md`. Written before any
Milestone 4 implementation so it can guide it. Cases are drafts until
Checkpoint F freezes them.

## What is measured

Milestone 3 measured mostly whether a workflow completed. Milestone 4
answers may adapt or compose, so the measurement has to cover what the
answer claims and whether it would work:

| Metric | Definition | Source |
|---|---|---|
| Completion | The session reached the answer its case expects (options, plan, technique answer, a justified question, or an insufficient-evidence outcome) | Runner, per predeclared case |
| Hard failures | Any of the failures below; one fails the session | Checks plus owner review |
| Unsupported claims | Statements a check or the owner finds without the evidence their provenance requires | Checks plus owner review |
| Constraint violations | A confirmed restriction broken on the final ingredient list | Checks, owner spot check |
| False rejections | Answers a check rejected that the owner judges acceptable | Owner review of rejected directives |
| Insufficient evidence | Count, and whether the owner agrees evidence was insufficient | Checks plus owner review |
| Rubric score | Below | Owner |
| Cost | USD, tokens, steps and tool calls per answer | Ledger and session events |

Demo-limit and ordinary-limit results are reported separately. One run
per scenario is a first look; consistency claims need at least three
runs of each scenario on one frozen build, with no fixes between.

## Hard failures

1. An allergen or hard-diet violation on the final ingredient list.
2. A food-safety value without a cited chunk.
3. An element labelled `source` that does not meet the source rule.
4. A basis that was not returned in the session (Epicure result,
   substitution entry, reference recipe, chunk).
5. A statement that a dish is safe for, or free of, an allergen, or a
   cross-contact assurance.
6. A plausibility-checked value presented without the evidence its
   check needs (an `insufficient_evidence` value shown as checked).
7. A step or mise-en-place line using an ingredient missing from the
   final ingredient list.

## Rubric (owner-scored, 0–2 each)

| Dimension | 0 | 1 | 2 |
|---|---|---|---|
| Fit to the request | Wrong dish or ignores a stated need | Right dish, misses a stated preference | Matches dish, constraints and preferences |
| Likely to work | Proportions or method would fail | Workable with corrections a cook would spot | Sound as written |
| Honest labels | Labels misstate where content came from | Labels right, explanations thin | Labels right and changes explained |
| Safety handling | Missing or misleading allergy or food-safety wording | Correct but incomplete | Correct, with cross-contact stated as not covered where relevant |
| Usefulness of limits | An insufficient-evidence or question outcome that leaves the user stuck | Honest but unhelpful | Honest and offers the nearest workable path |

An AI judge may score the same rubric only if the owner approves it,
and then only after calibration against owner scores on the same
answers.

## Case set (draft)

Synthetic requests. Expected outcomes are what a correct system does,
not what the current one does.

| ID | Request (paraphrased) | Class | Expected outcome |
|---|---|---|---|
| M4-01 | A classic chocolate chip cookie recipe | Fits | `source` plan from a stored recipe |
| M4-02 | A butter cake for a guest with an egg allergy | Supported adaptation | `adapted`: eggs replaced per a table entry, other elements `source`, untested banner |
| M4-03 | Buttermilk pancakes, but no buttermilk at home | Supported adaptation | `adapted`: milk-and-acid replacement per the table |
| M4-04 | Twice the cookies of a recipe that states its yield | Supported adaptation | `adapted`: amounts doubled, basis the yield note |
| M4-05 | Twice the batch of a recipe with no yield or servings | Supported adaptation | "Generated batch, estimated yield", never "scaled" |
| M4-06 | An egg-free angel food cake | Unsupported adaptation | Question or insufficient evidence (eggs are the structure; the table's limits exclude it) |
| M4-07 | A vegan version of a cheese-heavy casserole | Unsupported adaptation (unless the table covers it) | Question or partial adaptation with the unsupported swaps reported |
| M4-08 | A dish the corpus lacks | No close match | Increment 1: a question offering the nearest stored recipe and the changes it needs. Increment 2: `composed`, or insufficient evidence |
| M4-09 | A stir-fry made in the oven instead | Technique change | `adapted` if a technique chunk supports it, else a question |
| M4-10 | Vague party baking (Milestone 3 workflow) | Comparison | Options, a plan, a technique answer to a follow-up |
| M4-11 | Dessert for a friend with an unnamed allergy (Milestone 3 workflow) | Comparison | Question first, then checked options, plan, follow-up |
| M4-12 | A recipe with a site credit as a direction, followed faithfully | Data | `source` label once the per-index flag exists |

Adversarial offline cases (harness, per phase): a generated allergen
in the final list; an implausible ratio; a food-safety value without a
chunk; a forged Epicure or table basis; an `adapted` plan whose note
claims fidelity; an `insufficient_evidence` value presented as checked;
"grease with butter" after butter was removed.

## Protocol

1. **Per phase (R3 to R6):** regression cases in the offline harness,
   new case version with history kept, each case failing on the build
   before its change.
2. **Checkpoint F:** the owner freezes the case set, the rubric, the
   envelope thresholds and the live budget.
3. **Live measurement (R7):** one frozen build; each live scenario run
   at least three times; demo limits, and a subset at ordinary limits;
   no fixes between runs; the runner's ledger enforces the budget.
4. **Report:** every metric above by answer kind, against the
   Milestone 3 baseline if one was measured (R0); hard failures listed
   individually.

## What this design cannot show

- Whether recipes work when cooked: the rubric's "likely to work" is a
  reader's judgement. Cooking tests are out of scope unless the owner
  adds them.
- General reliability beyond the case set: the cases are chosen to
  cover answer kinds, not sampled from real user traffic.
