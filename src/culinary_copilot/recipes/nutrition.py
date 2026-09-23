"""Typed nutrition observations (Workstream 1B).

The legacy API exposes ``nutrition: {"fat": 2.5, "sodium": 29.8}`` with no
units or serving basis. This module adds typed observations while keeping the
legacy float map for compatibility.

Policy (no guessing):
- Units/basis are ``unknown`` unless upstream documentation supports them.
- AkashPS11 absolute columns (Calories, FatContent, ...): card README is
  effectively empty; no unit/basis statement was located as of 2026-09-22, so
  unit/basis stay ``unknown`` with recorded evidence. Field-name inference is
  deliberately NOT applied.
- jojogo9 7-element list semantics (calories=kcal, rest=%DV per serving on a
  2000-kcal diet) are documented by the related derivative
  tiptoghosh/food-recipes-15k README ("Nutrition Values — Important Note"):
  https://huggingface.co/datasets/tiptoghosh/food-recipes-15k
  That evidence applies to that list encoding only and is NOT transferred to
  AkashPS11 absolute columns.
- %DV values are kept distinct from g/mg via ``unit="percent_daily_value"``.
- Valid zeros are preserved. Nonfinite (NaN/Inf), invalid negatives and
  malformed strings are flagged as quality issues, never silently mapped to
  plain missing values.
- No nutrition is calculated from ingredients here.
- Diet keywords are never interpreted as dietary compliance.
"""

import math
from typing import Any, Literal

NutrientUnit = Literal["g", "mg", "kcal", "kJ", "percent_daily_value", "unknown"]
NutrientBasis = Literal["per_serving", "per_recipe", "per_100g", "unknown"]
SourceKind = Literal["reported", "calculated", "unknown"]
Verification = Literal["unverified", "supported", "invalid"]

# Legacy keys in document["nutrition"], mapped to their raw CSV columns.
NUTRITION_FIELDS: dict[str, str] = {
    "calories": "Calories",
    "fat": "FatContent",
    "saturated_fat": "SaturatedFatContent",
    "cholesterol": "CholesterolContent",
    "sodium": "SodiumContent",
    "carbohydrate": "CarbohydrateContent",
    "fiber": "FiberContent",
    "sugar": "SugarContent",
    "protein": "ProteinContent",
}

MISSING_TOKENS = {"", "NA", "NULL"}

UNKNOWN_BASIS_EVIDENCE = (
    "No unit/serving-basis statement located in AkashPS11/recipes_data_food.com "
    "card (21-byte README, license only) as of 2026-09-22; field-name inference "
    "not applied. Related derivative tiptoghosh/food-recipes-15k documents "
    "kcal-vs-%DV for its 7-element nutrition list only, not for these columns."
)
UNKNOWN_BASIS_URLS = [
    "https://huggingface.co/datasets/AkashPS11/recipes_data_food.com",
    "https://huggingface.co/datasets/tiptoghosh/food-recipes-15k",
]


def parse_nutrition_value(raw_value: str) -> tuple[float | None, str | None]:
    """Return (value, problem_code).

    problem_code is None on success/missing; otherwise one of
    ``malformed_nutrition``, ``nonfinite_nutrition``, ``negative_nutrition``.
    Missing tokens (blank/NA/NULL) yield (None, None) — ordinary absence.
    """
    text = (raw_value or "").strip()
    if text in MISSING_TOKENS:
        return None, None
    try:
        value = float(text)
    except ValueError:
        return None, "malformed_nutrition"
    if not math.isfinite(value):
        return None, "nonfinite_nutrition"
    if value < 0:
        return None, "negative_nutrition"
    return value, None


def observe_foodcom_nutrition(
    raw: dict[str, str],
    *,
    serving_amount: float | None = None,
    serving_unit: str | None = None,
) -> tuple[dict[str, float | None], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build legacy map + typed observations + nutrition quality issues."""
    legacy: dict[str, float | None] = {}
    observations: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for nutrient, column in NUTRITION_FIELDS.items():
        source_raw = raw.get(column, "")
        value, problem = parse_nutrition_value(source_raw)
        legacy[nutrient] = value
        if problem is not None:
            issues.append(
                {
                    "code": problem,
                    "severity": "warning",
                    "field": column,
                    "message": f"{column}={source_raw.strip()!r}: not a valid "
                    "non-negative finite number; kept as missing with a flag.",
                }
            )
            observations.append(
                {
                    "nutrient": nutrient,
                    "value": None,
                    "unit": "unknown",
                    "basis": "unknown",
                    "serving_amount": serving_amount,
                    "serving_unit": serving_unit,
                    "source_field": column,
                    "source_value_raw": source_raw.strip(),
                    "source_kind": "reported",
                    "verification": "invalid",
                    "evidence": f"Rejected: {problem}. {UNKNOWN_BASIS_EVIDENCE}",
                    "evidence_urls": UNKNOWN_BASIS_URLS,
                }
            )
            continue
        observations.append(
            {
                "nutrient": nutrient,
                "value": value,
                "unit": "unknown",
                "basis": "unknown",
                "serving_amount": serving_amount,
                "serving_unit": serving_unit,
                "source_field": column,
                "source_value_raw": source_raw.strip(),
                "source_kind": "reported",
                "verification": "unverified",
                "evidence": UNKNOWN_BASIS_EVIDENCE,
                "evidence_urls": UNKNOWN_BASIS_URLS,
            }
        )
    return legacy, observations, issues
