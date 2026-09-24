# Retrieval evaluation rubric v1 (phase1-rubric-v1)

Status: AI-authored under the owner-accepted calibration policy
(`calibration_owner_decisions.json`, acceptance v1). Individual recipe
judgments that cite this rubric remain AI-proposed unless a separate
human-verification record exists. Acceptance of the review is not
independent human source verification or culinary validation.

## 1. Topical relevance (graded 0/1/2, dish/ingredient match only)

- **2 — same dish:** the recipe is the requested dish or a named variant
  (e.g. "Curried Chicken" for chicken curry; "Paneer Tikka" is not a kebab
  by name, so it does not earn 2 for a kebab request on naming alone).
- **1 — same dish family:** shares the dish-defining characteristics but is
  not the dish (e.g. Cioppino for bouillabaisse; chicken soup is not
  automatically 1 for chicken curry — sharing one ingredient is not enough).
- **0 — different dish:** no dish-level match (e.g. apple pastry bites for
  apple pie; pork soup for a vegetable-soup request).

Topical grades never consider constraints, time, or equipment.

## 2. Constraint evidence (supported / violated / unresolved / not_applicable)

Judged against **displayed source evidence**: the stored recipe's
ingredient list, instructions, description, and reported metadata as shown
in the packet. Rules:

- **supported:** the displayed evidence affirmatively satisfies the
  constraint (e.g. reported total 265 ≤ 300 for a time ceiling; no
  meat/fish/egg/dairy anywhere in the displayed ingredients for vegan).
- **violated:** the displayed evidence contradicts the constraint (e.g.
  shrimp and butter against vegan; a two-week steep against a ready-to-use
  deadline).
- **unresolved:** the evidence is insufficient either way (e.g. spaghetti
  not specified gluten-free; servings not reported). "No violation found"
  is recorded as an observation and is **not** automatically `supported`.
- **not_applicable:** the request states no such constraint.

Source-supported does **not** mean independently verified, allergy-safe, or
complete. An explicit alternative — the verified-completeness prerequisite,
under which any incompleteness forces `unresolved` — is documented but not
adopted, because it would make nearly every record unjudgeable. Where the
stricter reading would differ, the adjudication records both.

## 3. System enforcement (per constraint, enforced / carried-unchecked)

What the retrieval system itself does with the constraint, independent of
the source evidence: time ceilings and dataset scope are enforced;
dietary, equipment, cuisine, preference, and substitution constraints are
carried through but not checked at retrieval. This column is a system
fact, not a judgment.

## 4. Overall suitability (supported / not suitable / unresolved / not_applicable)

Aggregation of the constraint evidence for the original recipe as written:

- any `violated` → **not suitable**;
- otherwise any `unresolved` → **unresolved**;
- all requested constraints `supported` → **supported**;
- no requested constraints → **not_applicable**.

A topical grade of 0 additionally forces **not suitable**: there is nothing
to recommend regardless of constraints.

Reported-time comparison is distinct from practical elapsed-time
feasibility: a reported positive total is not proof that the recipe fits a
real-world deadline (marinades, steeping, and untimed steps count). Raw
values are preserved; times are never summed from instructions, adapted,
or invented. Unavailable equipment is never inferred.

## 5. No-match discipline

An empty annotation pool is never a verified no-match case: it reflects
one query/filter/dataset slice of judged candidates, not production
retrieval output and not the corpus. No-match conclusions require the
production-equivalent query output plus a statement of what "no match"
means for that request (abstain vs clarify), which the packet records
separately.
