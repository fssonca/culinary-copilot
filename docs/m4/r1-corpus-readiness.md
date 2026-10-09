# Milestone 4, R1: corpus readiness and data profile

Run 2026-10-09, read-only against the application database (15,875
recipes: 14,659 foodie, 1,216 food.com) after the H5 cleanup. Scripts in
`scripts/m4/`, rule tests in `tests/test_m4_r1_scripts.py`. Per-row
outputs stay local under `data/m4-r1/` (they hold corpus text); this
report gives counts and the classification. No database writes, no
paid calls, no check changes.

## 1. Non-instruction directions, per direction index

`scripts/m4/direction_audit.py` takes every direction of at most three
words, plus any direction matching a credit or site pattern at any
length, and classifies it with the rule that decided.

| Class | Directions | Recipes | Distinct texts | Flag? |
|---|---:|---:|---:|---|
| Real step (`step`) | 1,365 | 1,160 | 527 | No: "Serve immediately.", "Mix well.", "Gather all ingredients." |
| Site or photo credit (`site_name`) | 593 | 120 | 42 | Yes: a site name with a photographer, often mid-list |
| Name-like (`name_like`) | 602 | 574 | 398 | Review first: mostly author names and usernames, one place name |
| Heading (`heading`) | 212 | 183 | 109 | Yes, as a heading (notes, tips, "To use:"); not a step |
| Sign-off (`sign_off`) | 132 | 132 | 4 | Yes |
| Placeholder (`placeholder`) | 56 | 56 | 4 | Yes |
| Credit phrase (`credit_phrase`) | 24 | 21 | 23 | Yes ("photo by …", "recipe by …") |
| Yield note (`yield_note`) | 27 | 27 | 26 | Not a step, but useful: a scaling anchor |
| Unclear | 25 | 22 | 25 | Manual review |

Findings:

- Short does not mean junk: 1,365 short directions are real steps.
- Site and photo credits sit in the middle of direction lists (503 of
  593), so a recipe-level "last direction" rule would miss them.
- Name-like texts need review before flagging: they are short, letters
  only and lack a cooking word, which is evidence, not proof.
- Yield notes are anchors for scaling (ADR 0003) and should be kept as
  data, not dropped.

Proposed flag (Checkpoint E): per (recipe, direction index), with the
class and rule; plans may skip flagged indices without losing the
`source` label. Applying it to the application database needs an owner
decision and the H5-style procedure.

## 2. Faithful-copy sweep: attribution versus eligibility

`scripts/m4/faithful_copy_sweep.py` builds, for every recipe, a plan
copied from the recipe itself and runs the loop's plan checks. Steps are
the stored directions verbatim, every index cited; structured claims use
the amount exactly as `get_recipe` shows it. Three mise-en-place
variants:

| Variant | Shape | foodie recipes failing attribution | food.com |
|---|---|---:|---:|
| `original` | each ingredient's original line | 4 | 3 |
| `lines` | one "AMOUNT UNIT INGREDIENT" line each | 17 | 3 |
| `list` | one "Gather A, B, C." line (the live shape) | 419 | 3 |

Eligibility failures, expected and not check errors: 8,905 foodie and
651 food.com recipes include raw protein or eggs, and a copy without a
cited food-safety chunk is rightly rejected.

Classification of the attribution failures:

| Cause | Recipes | Check verdict |
|---|---:|---|
| **List attribution** (fails only in the `list` variant; each amount passes on its own line) | 402 foodie | **False positive** of the list heuristic. Sampled pattern: the amount is attached to the preceding item when the next ingredient's name shares a word with another ingredient ("apple" in "green apples" and "apple cider vinegar"), when digits sit in the gap ("93% lean"), or after a count-only item ("2 egg yolks", "1 pie crust") |
| **Count-with-size lines** ("1 (3 1/2) pound roast" stored as 1 pound) | 11 foodie (`lines`), also within the list failures | **Correct rejection**; source data error: the shown amount is wrong |
| **Unit absorbed into the name, or a size inside the amount** ("2 (28g) oz dark chocolate" stored as amount 2, no unit, name "oz dark chocolate") | 2 foodie | Source data error; the copy cannot be attributed |
| **Equipment listed as an ingredient** ("5 pint jars") | 1 foodie | False positive (the container word is not an ingredient name); minor |
| **No-word or duplicated directions** (a direction of one character; repeated directions) | 4 foodie, 3 food.com (`original` label failures) | Correct under the current rule; resolved by the per-index flag |

No failure was attributed to the structured quantity check once the
sweep copied the model-facing amount (an earlier sweep run that copied
raw amount text produced 1,289 such failures; they were artifacts of the
sweep, not of the check).

Recommendation (R3, after Checkpoint E): fix the list attribution for
the three sampled patterns, each with a regression case from a
synthetic recipe; repair or flag the count-with-size and absorbed-unit
records through an owner-approved data procedure. No check is relaxed
for the data errors.

## 3. Data profile for envelopes

`scripts/m4/data_profile.py`, per dataset:

| | foodie | food.com |
|---|---:|---:|
| Ingredient lines | 140,237 | 9,680 |
| Amount with a volume unit | 90,690 | 0 |
| Amount with a mass unit | 6,339 | 0 |
| Amount with another unit (clove, can, package…) | 7,276 | 0 |
| Amount, no unit (counts) | 29,720 | 2,518 |
| No amount | 5,166 | 7,162 |
| Amount the ingestion parser cannot read | 1,046 | 0 |
| Recipes with stated servings | 179 | 815 |
| Distinct recipes (title and ingredient set) | 14,658 | 1,216 |
| Ratio-ready (≥3 mass or volume lines), distinct | 13,128 | 0 |

Findings:

- Food.com contributes no ratio evidence.
- Foodie's comparable lines are mostly volumes (90,690 versus 6,339
  mass): comparing proportions across ingredients needs
  ingredient-specific densities, or comparisons within the volume
  family only.
- Near-duplicates are rare after the H5 cleanup (one pair in foodie).
- Servings are rare in foodie (179); yield notes in directions (27) and
  pan sizes are the other anchors.

## 4. Category map

Rule-based on title (first match), with method subtypes from
ingredients and title. Every assignment records its rule
(`data/m4-r1/category_map.jsonl`).

| Category (foodie) | Recipes | Ratio-ready |
|---|---:|---:|
| soup/stew | 889 | 837 |
| pie/tart | 836 | 785 |
| salad | 785 | 705 |
| sauce/dressing | 727 | 652 |
| bread | 628 | 609 |
| cookie | 543 | 526 |
| cake | 542 | 513 |
| pasta | 416 | 365 |
| rice/grain | 390 | 373 |
| beverage | 371 | 304 |
| roast/braise | 351 | 297 |
| other named categories (13) | 1,954 | 1,789 |
| **unknown** | **6,227 (42%)** | 5,373 |

Subtypes (both datasets): cake butter 316, oil 93, pound 58, from a mix
40, foam 21, other 77; bread quick 497, yeast 193; cookie drop or other
581.

Findings: title rules leave 42% of foodie uncategorized, and the
bread "quick" subtype likely includes yeast breads whose yeast is not a
listed ingredient line. Before envelopes (R4) the map needs a second
pass using ingredients and directions, and a reviewed sample per
category.

## What this means for Milestone 4

- Plausibility evidence exists for foodie bakes and several savoury
  categories, mainly as volumes; elsewhere `insufficient_evidence` will
  be the honest outcome.
- The list attribution false positives are the one confirmed check
  defect; they affect the shape models actually write and should be
  fixed early in R3.
- The source data errors (count-with-size, absorbed units) reach the
  model as wrong amounts today; they need a data procedure, not a
  looser check.
