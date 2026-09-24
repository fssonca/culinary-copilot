"""Pure adjudication helpers for the calibration packet (AI-authored).

These helpers implement the owner-accepted policy
(``calibration_owner_decisions.json``) and ``evals/rubric_v1.md``. They do
not fetch data and make no human judgments: every output they produce is
labelled AI-authored under the accepted policy.

The animal-ingredient screens below are conventional, transparent keyword
lists applied to whole-word tokens of displayed ingredient names. They
assist evidence gathering; the status rules in :func:`aggregate_suitability`
decide the outcome. ``eggplant`` never matches ``egg`` because matching is
whole-word.
"""

from __future__ import annotations

import re
from typing import Any

# Conventional indicators; documented here and in rubric_v1 §2.
MEAT_FISH_WORDS = frozenset(
    {
        "chicken",
        "beef",
        "pork",
        "bacon",
        "sausage",
        "turkey",
        "duck",
        "lamb",
        "veal",
        "fish",
        "tuna",
        "salmon",
        "shrimp",
        "crab",
        "lobster",
        "clam",
        "clams",
        "mussel",
        "oyster",
        "scallop",
        "anchovy",
        "meat",
        "poultry",
        "prosciutto",
        "pancetta",
        "chorizo",
        "pepperoni",
    }
)
EGG_DAIRY_WORDS = frozenset(
    {
        "egg",
        "eggs",
        "milk",
        "butter",
        "cheese",
        "cream",
        "yogurt",
        "mayonnaise",
        "ghee",
        "whey",
        "ricotta",
        "parmesan",
        "mozzarella",
    }
)

_WORD = re.compile(r"[a-z]+")


def _tokens(name: str) -> set[str]:
    return set(_WORD.findall(name.lower()))


def screen_ingredients(names: list[str]) -> dict[str, list[str]]:
    """Whole-word screen of displayed ingredient names.

    Returns the matched items per class. Empty matches mean no
    conventional indicator was displayed — not proof of absence from the
    true recipe (see the completeness caveat in rubric_v1 §2).
    """
    meat = sorted({n for n in names if _tokens(n) & MEAT_FISH_WORDS})
    dairy = sorted({n for n in names if _tokens(n) & EGG_DAIRY_WORDS})
    return {"meat_fish": meat, "egg_dairy": dairy}


def vegetarian_status(names: list[str]) -> tuple[str, str]:
    """(status, evidence) for a vegetarian constraint on displayed names."""
    hits = screen_ingredients(names)["meat_fish"]
    if hits:
        return "violated", f"displayed animal ingredients: {hits}"
    return (
        "supported",
        f"no conventional meat/fish word in {len(names)} displayed ingredient names "
        "(displayed-source-evidence standard; completeness not verified)",
    )


def vegan_status(names: list[str]) -> tuple[str, str]:
    """(status, evidence) for a vegan constraint on displayed names."""
    hits = screen_ingredients(names)
    violators = sorted(set(hits["meat_fish"]) | set(hits["egg_dairy"]))
    if violators:
        return "violated", f"displayed animal ingredients: {violators}"
    return (
        "supported",
        f"no conventional animal-product word in {len(names)} displayed ingredient names "
        "(displayed-source-evidence standard; completeness not verified)",
    )


def aggregate_suitability(topical_grade: int | None, checks: list[dict[str, str]]) -> str:
    """Aggregate constraint evidence to overall suitability (rubric_v1 §4)."""
    if topical_grade is not None and topical_grade < 1:
        return "not_suitable"
    if any(c.get("status") == "violated" for c in checks):
        return "not_suitable"
    if any(c.get("status") == "unresolved" for c in checks):
        return "unresolved"
    if not checks:
        return "not_applicable"
    return "supported"


def adjudication_layer(
    *,
    topical_grade: int | None,
    topical_reason: str,
    checks: list[dict[str, str]],
    system_enforced: dict[str, str],
    basis: str,
) -> dict[str, Any]:
    """Assemble one AI-authored adjudication record with fixed provenance."""
    return {
        "reviewer_type": "ai_policy_adjudication",
        "policy": "calibration-owner-acceptance-v1",
        "rubric": "phase1-rubric-v1",
        "human_verified": False,
        "topical_grade": topical_grade,
        "topical_reason": topical_reason,
        "constraint_checks": checks,
        "system_enforced": system_enforced,
        "suitability": aggregate_suitability(topical_grade, checks),
        "basis": basis,
    }
