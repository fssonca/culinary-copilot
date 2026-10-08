# Checkpoint D: H6 judgment review sheet

Prepared 2026-10-08 with AI assistance, for decision 1 in
`docs/checkpoint-d-decisions.md`. The grades and reasons below are
AI-prepared and pending your review; they come from
`evals/results/h6/additional_judgments_v1.json` (sha256
`2e79ee34bf7647a9739f499976a91cfa9e762118c72705b2594c840601924c52`).

How to fill it in: in the "Your grade" column, write "ok" to accept the
AI grade, or the grade you would give. Add a note when it helps. The
filled sheet becomes version 2 of the judgment layer; the original
Phase 1 labels are never edited.

## Rubric (from `evals/rubric_v1.md`)

- **2, same dish:** the recipe is the requested dish or a named variant.
- **1, same dish family:** shares the dish-defining characteristics but
  is not the dish; sharing one ingredient is not enough on its own.
- **0, different dish:** no dish-level match.
- Topical grades never consider constraints, time or equipment. Those
  are recorded separately as suitability (`supported`, `violated`,
  `unresolved`, `not_applicable`).

Precedent for single-ingredient requests: in DEV-15 ("garlic") the
original labels gave grade 2 to dishes named after garlic (for example
garlic cauliflower, garlic butter potatoes, a garlic basil sauce). In
DEV-18 and DEV-19 the original labels only held recipes where garlic is
one ingredient among many, graded 1.

## The 18 AI-prepared judgments

Split "held" marks held-out cases, which are meant to test a choice made
on the development cases.

| # | Case | Split | Request | Recipe (dataset:source) | AI grade | Suitability | AI reason | Your grade | Note |
|---|---|---|---|---|---|---|---|---|---|
| 1 | DEV-08 | dev | pasta (pantry: garlic, olive oil, parmesan) | Chicken Parmesan Pasta Casserole (odunola/foodie:foodie-004166) | 2 | not_applicable | A pasta casserole for a generic pasta request; pasta names the dish. | | |
| 2 | DEV-15 | dev | garlic (25 min) | Honey and Garlic Dressing (AkashPS11/recipes_data_food.com:002516) | 2 | supported | Named after garlic, like the recorded garlic dressings and sauces. | | |
| 3 | DEV-18 | dev | garlic | Couscous Cakes With Tomato-Garlic Ragout (AkashPS11/recipes_data_food.com:000461) | 1 | not_applicable | Garlic seasons the ragout; the dish is couscous cakes, so ingredient overlap only. | | |
| 4 | DEV-18 | dev | garlic | Garlic Pizza Crust (AkashPS11/recipes_data_food.com:000556) | 2 | not_applicable | Named after garlic, like the recorded garlic-forward starches. | | |
| 5 | DEV-19 | dev | garlic | Air Fryer Honey Garlic Chicken Wings (odunola/foodie:foodie-012166) | 2 | not_applicable | Named after garlic, for a garlic request. | | |
| 6 | DEV-20 | dev | apple pie | Mum's Irish Apple Pie (odunola/foodie:foodie-003851) | 2 | not_applicable | An apple pie, like the recorded apple pies. | | |
| 7 | DEV-25 | dev | chicken dinner (pantry: chicken; equipment: wok) | Slow Cooker Chicken and Quinoa Dinner (odunola/foodie:foodie-017323) | 2 | unresolved | A chicken dinner, like the recorded chicken dinners; wok use not extracted. | | |
| 8 | DEV-25 | dev | chicken dinner (pantry: chicken; equipment: wok) | Mediterranean Chicken Sheet Pan Dinner (odunola/foodie:foodie-010415) | 2 | unresolved | A chicken dinner, like the recorded chicken dinners; wok use not extracted. | | |
| 9 | DEV-27 | dev | apple pie | Mum's Irish Apple Pie (odunola/foodie:foodie-003851) | 2 | not_applicable | An apple pie. | | |
| 10 | DEV-28 | dev | soup (pantry: cabbage, onion, carrots, celery, tomato, garlic, potato) | Beef Short Rib French Onion Soup (odunola/foodie:foodie-009890) | 2 | not_applicable | A soup, like the recorded soups. It needs beef, which the pantry lacks; topical grades ignore that. | | |
| 11 | DEV-29 | dev | soup (60 min) | Egg Drop Soup (AkashPS11/recipes_data_food.com:000619) | 2 | supported | A soup within the time limit. | | |
| 12 | HELD-09 | held | chocolate cake | Chocolate Tres Leches Cake (odunola/foodie:foodie-002654) | 2 | not_applicable | A chocolate cake. | | |
| 13 | HELD-09 | held | chocolate cake | German Chocolate Cake (odunola/foodie:foodie-015380) | 2 | not_applicable | A chocolate cake. | | |
| 14 | HELD-09 | held | chocolate cake | Swedish Sticky Chocolate Cake (Kladdkaka) (odunola/foodie:foodie-004765) | 2 | not_applicable | A chocolate cake. | | |
| 15 | HELD-14 | held | soup (45 min) | Egg Drop Soup (AkashPS11/recipes_data_food.com:000619) | 2 | supported | A soup within the time limit. | | |
| 16 | HELD-17 | held | tofu vegetable | Tofu Vegetable Pot Pie (odunola/foodie:foodie-006843) | 2 | not_applicable | Tofu and vegetables name the dish. | | |
| 17 | HELD-17 | held | tofu vegetable | Panang Curry with Tofu and Vegetables (odunola/foodie:foodie-011321) | 2 | not_applicable | Tofu and vegetables in the dish. | | |
| 18 | HELD-17 | held | tofu vegetable | Korean Tofu and Vegetable Soup (odunola/foodie:foodie-001553) | 2 | not_applicable | Tofu and vegetables in the dish. | | |

The reasons are shortened from the file; the file holds the full text.
The beef remark in row 10 is added here for review and is not in the
file.

## Five recipes only the title boost surfaces (not judged yet)

The boost's results count these as not relevant until they are judged.

| # | Case | Split | Request | Recipe (dataset:source) | Rank under the boost | Your grade | Suitability | Note |
|---|---|---|---|---|---|---|---|---|
| 19 | DEV-03 | dev | Cabbage Soup | Grandma's Canned Corned Beef and Cabbage Soup (odunola/foodie:foodie-014725) | 5 | | | |
| 20 | DEV-24 | dev | pasta (vegan, gluten-free) | Pasta e Fagioli (Pasta and Beans) (odunola/foodie:foodie-010409) | 5 | | | Check the pasta against gluten-free. |
| 21 | DEV-28 | dev | soup (pantry: cabbage, onion, carrots, celery, tomato, garlic, potato) | Traditional Bulgarian Soup (Shopi Style Soup) (odunola/foodie:foodie-000315) | 3 | | | |
| 22 | HELD-07 | held | chicken | Ashanti Chicken (Whole Deboned Chicken With rice) (odunola/foodie:foodie-015051) | 1 | | | |
| 23 | HELD-09 | held | chocolate cake | Chocolatetown Special Cake (Chocolate Cake) (AkashPS11/recipes_data_food.com:002495) | 2 | | | |

To see a recipe's ingredients and directions before grading, look it up
by its dataset and source id in the `recipes` table (read-only).
