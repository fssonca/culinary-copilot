"""Typed selection propositions: prerequisites and server-rendered wording.

Replaces the free-text ``selection_reasons`` / ``needs`` contract, whose
arbitrary text could publish unsupported claims (for example "Guaranteed
allergen-free and safe for everyone. Double every ingredient to serve
eight." passed validation). The model now proposes only an allowlisted
type, plus bounded source-local ingredient references for the one type
that needs them. This module checks each type's prerequisites against
authoritative request state and the selected source, then renders the
public wording itself. Nothing the model writes is displayed.

Outcome policy:
- A proposition whose prerequisites fail is omitted and recorded in
  ``rejected`` by kind, index, type, and a stable rejection code. No
  free text is echoed: the type is an allowlisted enum value.
- Omitting optional propositions never invalidates a valid selection; a
  recommendation may carry no reasons and no questions.
- Schema violations, invalid selections, and hard-constraint failures are
  handled by the caller and remain controlled failures.

Allowlist (reason types):

``dish_named_in_title``
    Requires a requested dish and every dish word (plural-aware) in the
    selected source title. Wording states a title-word match only, never
    that the recipe suits the request.
``uses_listed_ingredients``
    Requires listed available ingredients and 1..6 distinct refs, each an
    ingredient of the selected source whose name contains every word of
    some listed item (plural-aware; curated compounds such as "coconut
    milk" and free-from qualified names such as "gluten-free pasta" do
    not match a plain item). Wording names the matched source ingredients
    and states the source's total and non-optional counts, so available
    ingredients are never presented as covering the recipe.
``reported_time_within_limit``
    Requires an explicit request time limit and a usable (finite,
    positive) source-reported total within it. Wording reports both
    figures as source-reported and never promises practical time.
``stated_yield_matches_portions``
    Requires explicit requested portions and a known source yield equal to
    them. Wording states both numbers and that nothing was scaled.

Allowlist (question types, asked of the user about the user's request):

``desired_portions``
    Requires portions missing from request state (status ``unknown``) and a
    known source yield, the only thing the answer can be compared with (no
    scaling exists). An unknown source yield is never a reason to ask.
``time_available``
    Requires the time limit missing from request state; the answer narrows
    future searches (time is the only enforced search filter besides the
    dish/pantry query).
``dietary_restrictions``
    Requires dietary constraints missing from request state. Wording says
    the answer does not verify the source.
``available_ingredients``
    Requires available ingredients missing from request state.

No question type asks the user to supply facts about the recipe (its
yield, duration, allergens, nutrition): a user's answer cannot verify a
source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from culinary_copilot.domain.clarification import FieldStatus
from culinary_copilot.domain.recommendations import SelectionProposal
from culinary_copilot.recipes.durations import DurationStatus, satisfies_ceiling
from culinary_copilot.recommendations.evidence import strict_number
from culinary_copilot.recommendations.policy import COMPOUND_EXCEPTIONS, FREE_FROM_QUALIFIERS

# Stable rejection codes (rejected_propositions[].code).
REJECT_DUPLICATE = "duplicate_proposition"
REJECT_REFS_NOT_PERMITTED = "references_not_permitted"
REJECT_REFS_REQUIRED = "references_required"
REJECT_REF_NOT_IN_SOURCE = "reference_not_in_source"
REJECT_REF_DUPLICATE = "reference_duplicate"
REJECT_REF_NOT_LISTED = "reference_not_listed_ingredient"
REJECT_DISH_MISSING = "request_dish_missing"
REJECT_TITLE_MISMATCH = "title_lacks_dish_words"
REJECT_PANTRY_MISSING = "request_ingredients_missing"
REJECT_TIME_LIMIT_MISSING = "request_time_limit_missing"
REJECT_SOURCE_TIME_UNKNOWN = "source_time_unknown"
REJECT_SOURCE_TIME_EXCEEDS = "source_time_exceeds_limit"
REJECT_PORTIONS_MISSING = "request_portions_missing"
REJECT_SOURCE_YIELD_UNKNOWN = "source_yield_unknown"
REJECT_YIELD_DIFFERS = "source_yield_differs"
REJECT_FIELD_NOT_MISSING = "request_field_not_missing"

REASON_PREREQUISITES: dict[str, str] = {
    "dish_named_in_title": (
        "Request dish present; every dish word appears in the selected source title."
    ),
    "uses_listed_ingredients": (
        "Listed available ingredients present; 1-6 distinct refs, each a selected-source "
        "ingredient whose name contains every word of a listed item."
    ),
    "reported_time_within_limit": (
        "Explicit request time limit; usable source-reported total within it."
    ),
    "stated_yield_matches_portions": (
        "Explicit requested portions; known source yield equal to them."
    ),
}
QUESTION_PREREQUISITES: dict[str, str] = {
    "desired_portions": "Portions missing from request state; source yield known.",
    "time_available": "Time limit missing from request state.",
    "dietary_restrictions": "Dietary constraints missing from request state.",
    "available_ingredients": "Available ingredients missing from request state.",
}
# Request field each question type asks about.
_QUESTION_FIELD = {
    "desired_portions": "portions",
    "time_available": "time_minutes",
    "dietary_restrictions": "dietary_constraints",
    "available_ingredients": "ingredients",
}


@dataclass(frozen=True)
class RequestFacts:
    """Authoritative request values used by prerequisites and wording."""

    dish: str | None
    pantry: tuple[str, ...]
    time_ceiling: float | None
    portions: float | None
    field_status: dict[str, str] = field(default_factory=dict)

    def missing(self, target: str) -> bool:
        """True only when the field was never answered (status ``unknown``).

        Provided, no-preference, skipped, and conflicting fields are not
        re-asked; an absent status is treated as unknown.
        """
        return self.field_status.get(target, FieldStatus.UNKNOWN.value) == (
            FieldStatus.UNKNOWN.value
        )

    def missing_fields(self) -> list[str]:
        return [f for f in _QUESTION_FIELD.values() if self.missing(f)]


def request_facts(
    state: Any, *, dish: str | None, pantry: list[str], time_ceiling: float | None
) -> RequestFacts:
    """Build request facts from authoritative clarification state."""
    raw_portions = state.values.get("portions", state.request.get("portions"))
    portions = strict_number(raw_portions, allow_zero=False)
    status = {str(k): getattr(v, "value", str(v)) for k, v in state.field_status.items()}
    return RequestFacts(
        dish=dish.strip() if isinstance(dish, str) and dish.strip() else None,
        pantry=tuple(pantry),
        time_ceiling=time_ceiling,
        portions=portions,
        field_status=status,
    )


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _word_in(word: str, words: list[str]) -> bool:
    """Plural-aware whole-word membership (word, word+s, word+es, and back)."""
    forms = {word, word + "s", word + "es"}
    if word.endswith("es"):
        forms.add(word[:-2])
    if word.endswith("s"):
        forms.add(word[:-1])
    return any(w in forms for w in words)


def _all_words_in(needle: str, haystack: str) -> bool:
    needle_words = _words(needle)
    hay_words = _words(haystack)
    return bool(needle_words) and all(_word_in(w, hay_words) for w in needle_words)


def _matches_listed(canonical: str, listed: str) -> bool:
    """Ingredient name contains every word of a listed item, excluding
    curated compounds and free-from qualified names unless the listed item
    names them too (a listed "milk" never matches "coconut milk")."""
    name = " ".join(canonical.lower().split())
    item = " ".join(listed.lower().split())
    if not _all_words_in(item, name):
        return False
    for phrase in COMPOUND_EXCEPTIONS | FREE_FROM_QUALIFIERS:
        if phrase in name and phrase not in item:
            return False
    return True


def _fmt(number: float) -> str:
    return f"{number:g}"


def _render_reason(
    kind: str, refs: list[str], candidate: dict[str, Any], facts: RequestFacts
) -> tuple[str | None, str | None]:
    """Return ``(wording, None)`` when prerequisites hold, else ``(None, code)``."""
    ingredients = {str(i.get("ref")): i for i in candidate.get("ingredients", [])}
    if kind != "uses_listed_ingredients" and refs:
        return None, REJECT_REFS_NOT_PERMITTED
    if kind == "dish_named_in_title":
        if not facts.dish:
            return None, REJECT_DISH_MISSING
        title = str(candidate.get("title") or "")
        if not _all_words_in(facts.dish, title):
            return None, REJECT_TITLE_MISMATCH
        return (
            f'The source title "{title}" contains every word of your requested dish. '
            "This is a title match, not a judgement that the recipe suits your request.",
            None,
        )
    if kind == "uses_listed_ingredients":
        if not facts.pantry:
            return None, REJECT_PANTRY_MISSING
        if not refs:
            return None, REJECT_REFS_REQUIRED
        if len(set(refs)) != len(refs):
            return None, REJECT_REF_DUPLICATE
        names: list[str] = []
        for ref in refs:
            item = ingredients.get(ref)
            if item is None:
                return None, REJECT_REF_NOT_IN_SOURCE
            canonical = str(item.get("canonical") or "")
            if not any(_matches_listed(canonical, listed) for listed in facts.pantry):
                return None, REJECT_REF_NOT_LISTED
            names.append(f"{canonical} ({ref})")
        total = len(ingredients)
        required = sum(1 for i in ingredients.values() if not i.get("optional"))
        return (
            "Source ingredients matching items you listed as available: "
            + ", ".join(names)
            + f". The source lists {total} ingredient(s), {required} not marked optional; "
            "this does not mean you have everything the recipe needs.",
            None,
        )
    if kind == "reported_time_within_limit":
        if facts.time_ceiling is None:
            return None, REJECT_TIME_LIMIT_MISSING
        reported = candidate.get("total_minutes_reported")
        if candidate.get("duration_status") != DurationStatus.REPORTED_POSITIVE.value or not (
            isinstance(reported, (int, float)) and not isinstance(reported, bool)
        ):
            return None, REJECT_SOURCE_TIME_UNKNOWN
        if not satisfies_ceiling(reported, facts.time_ceiling):
            return None, REJECT_SOURCE_TIME_EXCEEDS
        return (
            f"The source reports a total time of {_fmt(float(reported))} minutes, within "
            f"your {_fmt(facts.time_ceiling)}-minute limit. This is the source's own figure, "
            "not a promise of how long it will take you.",
            None,
        )
    if kind == "stated_yield_matches_portions":
        if facts.portions is None:
            return None, REJECT_PORTIONS_MISSING
        servings = candidate.get("servings") if candidate.get("servings_known") else None
        if not isinstance(servings, (int, float)) or isinstance(servings, bool):
            return None, REJECT_SOURCE_YIELD_UNKNOWN
        if float(servings) != facts.portions:
            return None, REJECT_YIELD_DIFFERS
        return (
            f"The source states a yield of {_fmt(float(servings))} servings, the same as the "
            f"{_fmt(facts.portions)} portions you asked for. Nothing was scaled.",
            None,
        )
    # Unreachable for schema-valid proposals (the type is an enum).
    raise ValueError(f"unknown reason type {kind!r}")


def _render_question(
    kind: str, candidate: dict[str, Any], facts: RequestFacts
) -> tuple[str | None, str | None]:
    target = _QUESTION_FIELD[kind]
    if not facts.missing(target):
        return None, REJECT_FIELD_NOT_MISSING
    if kind == "desired_portions":
        servings = candidate.get("servings") if candidate.get("servings_known") else None
        if not isinstance(servings, (int, float)) or isinstance(servings, bool):
            return None, REJECT_SOURCE_YIELD_UNKNOWN
        return (
            "How many portions would you like to make? The source states a yield of "
            f"{_fmt(float(servings))} servings; no scaling is performed.",
            None,
        )
    if kind == "time_available":
        return (
            "How much time do you have to cook? A time limit restricts future searches to "
            "sources whose reported total time fits within it.",
            None,
        )
    if kind == "dietary_restrictions":
        return (
            "Do you have any dietary restrictions or allergies? This source has not been "
            "checked for them, and your answer would not verify it.",
            None,
        )
    if kind == "available_ingredients":
        return (
            "Which ingredients do you already have? Listed ingredients help rank future "
            "searches; they are not checked against everything a recipe needs.",
            None,
        )
    raise ValueError(f"unknown question type {kind!r}")


def evaluate_propositions(
    proposal: SelectionProposal, candidate: dict[str, Any], facts: RequestFacts
) -> dict[str, Any]:
    """Accept or omit each proposition; never raises for unmet prerequisites.

    Returns ``{"reasons": [...], "questions": [...], "rejected": [...]}``.
    Accepted entries carry the type, validated refs, and server wording;
    rejected entries carry kind, index, type, and a stable code only.
    """
    reasons: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, reason in enumerate(proposal.reasons):
        refs = list(reason.ingredient_refs)
        if reason.type in seen:
            code: str | None = REJECT_DUPLICATE
            text = None
        else:
            text, code = _render_reason(reason.type, refs, candidate, facts)
        seen.add(reason.type)
        if text is None:
            rejected.append({"kind": "reason", "index": index, "type": reason.type, "code": code})
            continue
        reasons.append({"type": reason.type, "ingredient_refs": refs, "text": text})
    seen = set()
    for index, question in enumerate(proposal.questions):
        if question.type in seen:
            code = REJECT_DUPLICATE
            text = None
        else:
            text, code = _render_question(question.type, candidate, facts)
        seen.add(question.type)
        if text is None:
            rejected.append(
                {"kind": "question", "index": index, "type": question.type, "code": code}
            )
            continue
        questions.append({"type": question.type, "text": text})
    return {"reasons": reasons, "questions": questions, "rejected": rejected}
