"""Deterministic constraint policy for recommendations (Phase 3).

Constraints come from authoritative clarification state. Verdicts use the
typed policy ``supported`` / ``violated`` / ``unresolved`` /
``not_applicable`` shared with the retrieval rubric. Model assertions can
never promote a hard constraint to supported: this module computes verdicts
from source evidence only, and the service ignores any model-supplied
verdict (the proposal schema has no verdict field).

Narrowly justified source checks (documented limitations):
- Time ceiling: reuses the shared duration policy (only finite positive
  reported totals can satisfy a ceiling; unknown durations stay unknown).
  Reported durations remain source-reported; the service never invents
  actual cooking times or promises deadline feasibility beyond the support
  available (a reported total within the ceiling is ``supported`` as a
  source-reported value only).
- Dietary hard constraints: a candidate is ``violated`` only on a
  definitive whole-word contradiction between a canonical ingredient name
  and the diet (narrow per-diet token lists, plural-aware, with curated
  compound exceptions such as ``coconut milk`` or ``peanut butter`` whose
  meaning does not contain the prohibited food, and with qualified names
  such as ``gluten-free pasta`` treated as ``unresolved`` rather than
  decided). Every violation records the matched source ingredient
  reference, the matched token, and an explanation. ABSENCE of a match is
  ``unresolved``, never ``supported``: "no prohibited keyword found" does
  not establish dietary compatibility (hidden ingredients, cross-contact,
  and incomplete sources are out of scope for keyword checks). This is a
  limited heuristic, not dietary or allergy certification. Missing
  capabilities, nutrition units, servings, or dietary evidence remain
  unknown.
- Cuisine / preferences / equipment / substitution choices: the current
  search and corpus provide no enforcement evidence, so they are
  ``not_applicable`` here (preserved in state, reported as unverified).
- Portions/scaling: servings known only for finite non-boolean numerics;
  scaling feasibility itself is ``unresolved`` (no scaling in Phase 3).

Outcome rules:
- A known hard-constraint violation can never yield a successful
  recommendation (violated candidates are excluded from generation
  evidence; selecting one fails validation).
- An unresolved hard constraint needs clarification when the user can
  resolve it (conflicting requirements, missing task essentials); when the
  gap is source evidence no question can fix, the result is
  ``insufficient_evidence``.
"""

from __future__ import annotations

import re
from typing import Any

from culinary_copilot.domain.recommendations import (
    ConstraintAssessment,
    ConstraintVerdict,
)
from culinary_copilot.recipes.durations import satisfies_ceiling

# Narrow prohibited-token lists for hard-constraint violation checks.
# Whole-word match (plural-aware) against tokenized canonical names, with
# curated compound exceptions and qualifier handling below. Absence never
# implies compatibility (see module docstring).
_VEGAN_PROHIBITED = (
    "chicken",
    "beef",
    "pork",
    "fish",
    "bacon",
    "turkey",
    "lamb",
    "meat",
    "sausage",
    "ham",
    "shrimp",
    "crab",
    "milk",
    "cheese",
    "butter",
    "cream",
    "yogurt",
    "egg",
    "honey",
    "gelatin",
)
_VEGETARIAN_PROHIBITED = (
    "chicken",
    "beef",
    "pork",
    "fish",
    "bacon",
    "turkey",
    "lamb",
    "meat",
    "sausage",
    "ham",
    "shrimp",
    "crab",
    "gelatin",
)
_GLUTEN_FREE_PROHIBITED = (
    "wheat",
    "gluten",
    "barley",
    "rye",
    "flour",
    "bread",
    "pasta",
    "spaghetti",
    "noodle",
    "couscous",
    "semolina",
)

# Canonical diet identifiers with explicit known aliases. Anything else
# (custom or unknown restrictions) has no justified check and stays
# unresolved. Normalized with hyphens/spaces/compact spellings unified.
_DIET_ALIASES: dict[str, tuple[str, ...]] = {
    "vegan": ("vegan", "plant-based", "plant based", "dairy-free-vegan"),
    "vegetarian": ("vegetarian", "veggie", "veg"),
    "gluten-free": ("gluten-free", "gluten free", "glutenfree", "coeliac", "celiac"),
}

_HARD_DIET_TOKENS: dict[str, tuple[str, ...]] = {
    "vegan": _VEGAN_PROHIBITED,
    "vegetarian": _VEGETARIAN_PROHIBITED,
    "gluten-free": _GLUTEN_FREE_PROHIBITED,
}

# Compound names whose meaning does NOT contain the prohibited food even
# though a whole word matches (e.g. "milk" in "coconut milk"). Curated
# narrowly; absence from this list keeps the strict reading.
_COMPOUND_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "coconut milk",
        "coconut cream",
        "almond milk",
        "oat milk",
        "soy milk",
        "rice milk",
        "hemp milk",
        "peanut butter",
        "almond butter",
        "cashew butter",
        "cocoa butter",
        "shea butter",
        "rice flour",
        "almond flour",
        "coconut flour",
        "chickpea flour",
        "oat flour",
        "buckwheat flour",
    }
)

# Explicit free-from qualifiers: a qualified name ("gluten-free pasta",
# "vegan cheese") is ambiguous, substituted, or reformulated stock, so it
# is unresolved rather than decided by keyword.
_QUALIFIERS: frozenset[str] = frozenset(
    {
        "gluten-free",
        "gluten free",
        "vegan",
        "vegetarian",
        "dairy-free",
        "dairy free",
        "egg-free",
        "egg free",
        "plant-based",
        "plant based",
    }
)


# Public read-only views shared with proposition matching
# (recommendations/propositions.py), so a listed "milk" never matches
# "coconut milk" there either.
COMPOUND_EXCEPTIONS = _COMPOUND_EXCEPTIONS
FREE_FROM_QUALIFIERS = _QUALIFIERS


def _normalize_diet(diet: str) -> str | None:
    key = " ".join(diet.strip().lower().replace("-", " ").split())
    for canonical, aliases in _DIET_ALIASES.items():
        normalized_aliases = {" ".join(a.replace("-", " ").split()) for a in aliases}
        if key in normalized_aliases or key == canonical.replace("-", " "):
            return canonical
    return None


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", name.strip().lower()) if w]


def _token_hit(words: list[str], token: str) -> bool:
    """Whole-word match, plural-aware (token, token+s, token+es)."""
    forms = {token, token + "s", token + "es"}
    return any(w in forms for w in words)


def _dietary_match(diet: str, ref: str, canonical: str) -> tuple[bool, str | None, str | None]:
    """Return ``(violated, matched_token, explanation)`` for one ingredient.

    Definitive contradiction only: a whole-word (plural-aware) prohibited
    token in the canonical name, with qualified names and curated safe
    compounds excluded as ambiguous rather than decided.
    """
    normalized = _normalize_diet(diet)
    if normalized is None:
        return False, None, "Unknown or custom restriction; no justified check exists."
    tokens = _HARD_DIET_TOKENS[normalized]
    lowered = " ".join(canonical.strip().lower().split())
    if not lowered:
        return False, None, "Empty ingredient name; no determination possible."
    for qualifier in _QUALIFIERS:
        if qualifier in lowered:
            return (
                False,
                None,
                f"Qualified name {canonical!r} mentions {qualifier!r}; ambiguous or "
                "substituted stock, left unresolved rather than decided.",
            )
    for exception in _COMPOUND_EXCEPTIONS:
        if exception in lowered:
            return (
                False,
                None,
                f"Known compound {exception!r} in {canonical!r}; meaning does not "
                "contain the prohibited food, left unresolved.",
            )
    words = _words(lowered)
    for token in tokens:
        if _token_hit(words, token):
            return (
                True,
                token,
                f"Source ingredient {ref} ({canonical!r}) contains {token!r}, "
                f"contradicting {diet!r} (narrow keyword heuristic, not certification).",
            )
    return (
        False,
        None,
        (
            f"No prohibited token matched in {ref} ({canonical!r}); absence is not "
            "compatibility. Unverified."
        ),
    )


def assess_candidate(
    candidate: dict[str, Any],
    *,
    dietary_constraints: list[str],
    time_ceiling: float | None,
) -> list[ConstraintAssessment]:
    """Server-computed verdicts for one evidence candidate."""
    assessments: list[ConstraintAssessment] = []
    for diet in dietary_constraints:
        violated = False
        first_note = ""
        for item in candidate.get("ingredients", []):
            ref = str(item.get("ref") or "?")
            canonical = str(item.get("canonical") or "")
            hit, _token, explanation = _dietary_match(str(diet), ref, canonical)
            if hit:
                violated = True
                first_note = explanation or ""
                break
            # Preserve ambiguity explanations (qualified or compound names)
            # so unresolved verdicts say why instead of a bare no-match.
            if explanation and ("ualified" in explanation or "compound" in explanation):
                if not first_note:
                    first_note = explanation
        if violated:
            verdict = ConstraintVerdict.VIOLATED
            detail = first_note[:500] if first_note else f"Source evidence contradicts '{diet}'."
        else:
            verdict = ConstraintVerdict.UNRESOLVED
            detail = (
                (first_note + " " if first_note else "")
                + f"No determination for '{diet}': absence of a prohibited "
                "keyword is not dietary compatibility. Unverified."
            )[:500]
        assessments.append(
            ConstraintAssessment(constraint=f"dietary:{diet}", verdict=verdict, detail=detail)
        )
    if time_ceiling is not None:
        reported = candidate.get("total_minutes_reported")
        if satisfies_ceiling(reported, time_ceiling):
            assessments.append(
                ConstraintAssessment(
                    constraint="time_limit",
                    verdict=ConstraintVerdict.SUPPORTED,
                    detail=(
                        f"Source-reported total {reported:g} within ceiling "
                        f"{time_ceiling:g}. Source-reported only; not a "
                        "deadline-feasibility promise."
                    ),
                )
            )
        elif candidate.get("total_minutes_known"):
            assessments.append(
                ConstraintAssessment(
                    constraint="time_limit",
                    verdict=ConstraintVerdict.VIOLATED,
                    detail=(
                        f"Source-reported total {reported:g} exceeds ceiling {time_ceiling:g}."
                    ),
                )
            )
        else:
            assessments.append(
                ConstraintAssessment(
                    constraint="time_limit",
                    verdict=ConstraintVerdict.UNRESOLVED,
                    detail="No usable source-reported total duration; unknown.",
                )
            )
    for label in ("cuisine", "preferences", "equipment", "substitution_choice"):
        assessments.append(
            ConstraintAssessment(
                constraint=label,
                verdict=ConstraintVerdict.NOT_APPLICABLE,
                detail="Not enforced by retrieval or source evidence; preserved unverified.",
            )
        )
    return assessments


def hard_violation(assessments: list[ConstraintAssessment]) -> bool:
    return any(
        a.verdict is ConstraintVerdict.VIOLATED and a.constraint.startswith("dietary:")
        for a in assessments
    )


def unresolved_hard(assessments: list[ConstraintAssessment]) -> bool:
    return any(
        a.verdict is ConstraintVerdict.UNRESOLVED and a.constraint.startswith("dietary:")
        for a in assessments
    )
