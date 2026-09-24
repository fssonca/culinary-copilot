"""Deterministic request-to-query mapping (Phase 1, repaired per D1).

Eligibility versus ranking (owner decision D1):

- Dish + pantry: the dish text alone decides eligibility. Available pantry
  ingredients only influence ranking (a separate OR-scored boost in the
  repository); a dish match with zero pantry overlap remains eligible.
- Pantry-only: eligibility is overlap with at least one available
  ingredient (exact canonical names, so multiword meaning such as
  "olive oil" is preserved), ranked by overlap count. A match never
  implies the recipe needs no additional ingredients.
- There is currently no must-have ingredient field in the clarification
  state, so ``required_ingredients`` is always empty here. Explicit
  required-ingredient filters stay mandatory containment wherever the
  repository supports them; nothing here relaxes them.

Conservativeness: the mapping consumes structured state only, never raw
free-text messages. No stopword list is applied: dish terms, negation,
and restrictions are preserved verbatim in the query text (full-text
matching interprets them as text, not as logic). The original input is
preserved in ``dish_input`` and every transformation (whitespace
collapse, length/term caps) is recorded in ``transformations`` and the
explanation. No case-specific rules exist.

``time_minutes`` maps to the repository ``max_minutes`` ceiling; see
:mod:`culinary_copilot.recipes.durations` for the shared policy. Cuisine,
preferences, dietary constraints, equipment, substitution choices,
portions and task scope are preserved in state but never enforced by
full-text search; they are surfaced unchanged as
unsupported/unverified constraints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from culinary_copilot.domain.clarification import CookingRequestState
from culinary_copilot.services.clarification_service import SEARCH_UNENFORCED_TARGETS

MAX_PANTRY_TERMS = 5
MAX_QUERY_CHARS = 500
MAX_DISH_CHARS = 200

MATCH_DISH = "dish"
MATCH_PANTRY_OVERLAP = "pantry_overlap"


@dataclass(frozen=True)
class RetrievalQuery:
    """Deterministic translation of a ready request into search arguments."""

    query_text: str
    match_mode: str
    max_minutes: float | None
    dataset_id: str | None
    applied_filters: list[str] = field(default_factory=list)
    required_ingredients: list[str] = field(default_factory=list)
    pantry_hint_terms: list[str] = field(default_factory=list)
    match_any_ingredients: list[str] | None = field(default=None)
    rank_pantry_terms: list[str] = field(default_factory=list)
    unsupported_constraints: list[str] = field(default_factory=list)
    has_search_keys: bool = False
    dish_input: str = ""
    transformations: list[str] = field(default_factory=list)
    explanation: str = ""


def _dish_text(state: CookingRequestState) -> tuple[str, str]:
    """Return ``(verbatim_input, normalized)`` for the dish field."""
    raw = state.dish or state.values.get("dish") or ""
    verbatim = str(raw)
    normalized = " ".join(verbatim.split())
    return verbatim, normalized[:MAX_DISH_CHARS]


def _pantry(state: CookingRequestState) -> list[str]:
    raw = state.values.get("ingredients") or state.request.get("ingredients") or []
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            text = " ".join(str(item).split())
            if text:
                out.append(text[:MAX_DISH_CHARS])
    return out[:100]


def _time_ceiling(state: CookingRequestState) -> float | None:
    raw = state.values.get("time_minutes", state.request.get("time_minutes"))
    if raw is None or isinstance(raw, bool):
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 1 <= number <= 1440:
        return None
    return number


def map_request_to_query(
    state: CookingRequestState, *, dataset_id: str | None = None
) -> RetrievalQuery:
    """Map clarification state to repository search arguments."""
    verbatim_dish, dish = _dish_text(state)
    pantry = _pantry(state)
    ceiling = _time_ceiling(state)
    hint_terms = pantry[:MAX_PANTRY_TERMS]

    transformations: list[str] = ["whitespace-collapsed"]
    if len(" ".join(verbatim_dish.split())) > MAX_DISH_CHARS:
        transformations.append(f"dish-truncated-to-{MAX_DISH_CHARS}-chars")
    if len(pantry) > MAX_PANTRY_TERMS:
        transformations.append(f"pantry-ranking-terms-capped-at-{MAX_PANTRY_TERMS}")
    transformations.append("no-stopword-removal")

    if dish:
        match_mode = MATCH_DISH
        query_text = dish[:MAX_QUERY_CHARS]
        match_any = None
        rank_boost = hint_terms
    else:
        match_mode = MATCH_PANTRY_OVERLAP
        query_text = " ".join(hint_terms)[:MAX_QUERY_CHARS]
        match_any = hint_terms
        rank_boost = []

    applied: list[str] = []
    if ceiling is not None:
        applied.append("max_minutes")

    unsupported = sorted(
        target
        for target in SEARCH_UNENFORCED_TARGETS
        if state.field_status.get(target) is not None
        and state.field_status[target].value == "provided"
    )

    parts = []
    if dish:
        parts.append(f"dish={dish!r} (eligibility; verbatim, no term removal)")
        if hint_terms:
            parts.append(f"pantry-available ranking-only={hint_terms!r}")
    else:
        parts.append(
            f"pantry-overlap eligibility={hint_terms!r} "
            "(any overlap; more ingredients may still be needed)"
        )
    if ceiling is not None:
        parts.append(f"max_minutes<={ceiling:g}")
    if dataset_id is not None:
        parts.append(f"dataset={dataset_id}")
    else:
        parts.append("dataset=combined")
    parts.append(
        "required_ingredients=[] (no must-have field; explicit required filters stay mandatory)"
    )
    if unsupported:
        parts.append(f"unenforced-preserved={unsupported!r}")
    parts.append(f"transformations={transformations!r}")
    explanation = "Query mapping: " + "; ".join(parts) + "."

    return RetrievalQuery(
        query_text=query_text,
        match_mode=match_mode,
        max_minutes=ceiling,
        dataset_id=dataset_id,
        applied_filters=applied,
        required_ingredients=[],
        pantry_hint_terms=hint_terms,
        match_any_ingredients=match_any,
        rank_pantry_terms=rank_boost,
        unsupported_constraints=unsupported,
        has_search_keys=bool(dish or pantry),
        dish_input=verbatim_dish,
        transformations=transformations,
        explanation=explanation,
    )
