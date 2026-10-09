# ADR 0003: Stored recipes as references, not the answer

Status: **proposed** (2026-10-09), for Checkpoint E of
`docs/milestone-4-plan.md`. Supersedes nothing; ADR 0002 (agent loop)
stays in force. Evidence: `docs/m4/r1-corpus-readiness.md`.
Evaluation: `docs/m4/evaluation-design.md`.

## Context

Milestone 3 holds every answer to stored evidence: options are fetched
recipes, a plan comes from one selected recipe with exact amounts, and
any amount the source does not state is rejected. That keeps answers
trustworthy but leaves the model little to do: a request one ingredient
away from a stored recipe is dropped, a request with no close match ends
in a question, and Epicure shapes nothing. The owner set a new goal
(2026-10-08): stored recipes, technique chunks and Epicure become
references the model builds on, under explicit provenance.

R1 measured what the corpus can support. Comparable measurements come
almost only from foodie (food.com has no ingredient line with both an
amount and a unit), mostly as volumes, and title rules place 42% of
foodie recipes in no category. Plausibility checks will therefore often
lack evidence; the design has to make that a clear outcome, not a pass.

## Decision

### Answer kinds

| Kind | Built from | Use |
|---|---|---|
| `source` | One stored recipe | It fits as is |
| `adapted` | One stored recipe plus labelled changes | It fits after changes the evidence supports |
| `composed` | 2–5 fetched reference recipes | Nothing stored is close enough, or the user asks for something new (second increment, separate acceptance gate) |

Every finished answer also keeps today's kinds: options, technique
answers and web answers. A plan carries its kind.

### Provenance

Each final ingredient, step, quantity, time, temperature and claim has
a provenance and, unless `source`, a basis:

| Provenance | Meaning | Basis required |
|---|---|---|
| `source` | From the named stored recipe. Ingredients, amounts and titles exact; a step faithful to its cited direction (condensing allowed, a changed method not) | The source line or direction index |
| `adapted` | Changed from a source element | Reason, plus the evidence for the change: a substitution-table entry, a technique chunk, a scaling anchor |
| `generated` | Written by the model | Its evidence where any exists (reference recipes, envelope, chunk); may be none for wording and ordering |

### Check matrix

| Element | `source` | `adapted` | `generated` |
|---|---|---|---|
| Allergen / hard diet on the final ingredient list | strict | strict | strict |
| Food-safety values (internal temperature, holding time, raw handling) | strict: cite a chunk | strict: cite a chunk | strict: cite a chunk; never generated without one |
| Amounts | strict: exact | plausibility, with evidence | plausibility, with evidence |
| Oven temperature, bake time | strict: as the direction states | plausibility, with evidence | plausibility, with evidence |
| Step fidelity | strict: faithful to the cited direction | label only (the change is stated) | label only |
| Ingredient–text consistency | strict | strict | strict |
| Epicure attribution | strict: names a returned result | strict | strict |
| Substitution function and amount | — | strict: needs a table entry | strict: needs a table entry |
| Wording, order, plating | label only | label only | label only |

Outcomes per check: `pass`, `reject` (with the reason and, for
plausibility, the envelope), or `insufficient_evidence`. An
`insufficient_evidence` outcome is recorded on the element and shown;
it is never a pass. For a value that needs plausibility evidence and
has none, the answer either drops the value (with the reason), asks, or
falls back to a source value; it does not present the value as checked.

### Ingredient contract

A plan carries `final_ingredients`: every ingredient it uses, with
`name`, `amount`, `unit`, `provenance`, `basis`, and for compound
ingredients (cake mix, pie filling, stock, sauces) `components`, which
may be `unresolved`. Greasing, dusting and garnish ingredients are
included. Checks:

- every ingredient named in mise en place or steps is on the list, and
  every listed ingredient is used or has a reason;
- allergen and diet checks read the list; an unresolved component
  reports as unresolved and blocks any claim about that allergen;
- answers state that ingredient checks do not cover cross-contact
  (shared equipment, facility labelling).

### Compatibility

Answers carry `schema_version`. Milestone 3 answers are
`m3-legacy`: they keep their labels (`source` / `model_adaptation`
plans, technique answers, web answers), stay readable in the UI and in
exports, and are never relabelled with the new provenance. Only
`m4` answers carry the new fields. The Milestone 3 `source` label and
the new `source` provenance share the step rule (faithful to the cited
direction); the new one adds the complete ingredient list.

### Closeness measure

Computed by the loop from session evidence, not by the model alone:

- dish match: the request's dish words against the recipe title and
  category (R1 category map);
- changes needed: ingredients that violate a confirmed constraint, plus
  requested changes (pantry, technique, scale);
- supportability: whether each change has a substitution-table entry
  or a technique chunk.

Proposed rule (thresholds owner-set at Checkpoint E): no changes →
`source`; changes all supportable → `adapted`; otherwise ask, or (in
increment 2) `composed`. The model proposes a kind; the loop rejects a
kind the measure does not allow.

### Scaling

Scaling needs a batch-size anchor: stated servings or yield, pan size,
or piece count. Foodie states servings in 179 of 14,659 recipes, so the
anchor usually comes from a yield note, the pan, or the user. Without an
anchor the result is a "generated batch, estimated yield", labelled as
such, never "scaled".

### Substitution evidence

Epicure supplies candidates and flavour relationships, labelled
unverified as today. A swap also needs an entry in a cited substitution
table:

| Field | Example |
|---|---|
| `replaced` | egg (one large) |
| `function` | binder, leavening, moisture, emulsifier, structure, fat, acid |
| `replacement` | 1 tbsp ground flaxseed + 3 tbsp water |
| `amount_rule` | per egg replaced, binder role only |
| `limits` | not for more than 2 eggs; not where egg is the structure (soufflé, angel food) |
| `citation` | vetted source, with licence notes |

Entries are added to the technique corpus as documents with provenance,
after the owner approves the sources. Without a table entry, a swap may
appear only as an unverified idea without an amount, or the answer
asks.

### Never generated

Food-safety values without a cited chunk; any statement that a dish is
safe for, or free of, an allergen; cross-contact assurances; claims that
a recipe was tested; amounts attributed to a source that does not state
them.

### UI labels

Per element: "from the recipe", "changed: why", "suggested by the
assistant". Per answer: an "untested" banner for `adapted` and
`composed`; "insufficient evidence" shown plainly next to the value it
affects; the basis on demand.

### `AGENTS.md` wording (proposed)

Keep the data-integrity rule for the corpus and add one for answers:

> Unknown quantities, units, dietary compatibility, and nutrition in
> stored recipes remain unknown; never write generated values into the
> corpus. Answers may carry model-proposed values only when labelled
> with their provenance and checked as `docs/adr/0003-references-not-answers.md`
> requires; a value without the evidence its check needs is reported as
> insufficient evidence, never presented as checked.

## Worked examples

1. **Egg-free version of a stored butter cake.** The cake uses 2 eggs.
   The substitution table has a binder entry for up to 2 eggs in butter
   cakes. Kind `adapted`: the eggs become two flax eggs (`adapted`,
   basis: table entry); every other ingredient and step stays `source`;
   bake time stays `source` with a generated note to check doneness
   early (label only). A 4-egg sponge would fail the table's limits:
   the answer asks or offers a different stored recipe.
2. **Pantry swap.** No buttermilk; the table has milk plus lemon juice
   for buttermilk's acid role. `adapted`, amounts from the table.
3. **Doubled batch.** The user wants twice the cookies; the recipe
   states "makes about 24 cookies". Anchor found: every amount doubled
   (`adapted`, basis: yield note). Without the yield note: "generated
   batch, estimated yield", amounts doubled from the source, yield
   stated as an estimate.
4. **Dish the corpus lacks.** No stored recipe matches. Increment 1:
   the answer asks, offering the nearest stored recipe and the changes
   it would need. Increment 2: `composed` from 2–5 references, every
   amount checked against the references' envelope, or reported as
   insufficient evidence.

## Consequences

- More answers become possible, and more checks are needed: the
  ingredient contract and the substitution table are new
  infrastructure.
- `insufficient_evidence` will be common at first (thin, volume-only
  evidence; 42% of foodie uncategorized). The UI and the evaluation must
  treat it as an honest outcome.
- The Milestone 3 checks stay in force for `source` elements; relaxing
  any check needs a classified failure (R1). R1 classified one check
  defect (list attribution, 402 foodie recipes) to fix in R3; the other
  failures were source data errors or correct rejections.
- Composition is deferred to a second increment with its own gate.
