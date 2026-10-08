"""Deterministic scaling and unit conversion (Milestone 3, Phase 2).

``scale_recipe`` scales only quantities parsed with a known unit
(``canonical_unit`` from the closed vocabulary in
``recipes/adapters/foodie.py``). Unknown or unparsed quantities stay
unknown and are listed as such — never guessed, never dropped
silently. A missing/unknown servings count returns a typed refusal
(reason ``scale_missing_servings``).

``convert_units`` converts within compatible dimension groups
(mass <-> mass, volume <-> volume). Unknown units or cross-group
conversions return a typed refusal (reason
``convert_unsupported_unit``). Every conversion result states its unit
system (``metric`` / ``us_customary``); ``count`` units never convert.
Scaled items with qualitative units (pinch, dash, ...) carry
``approximate: true``. No nutrition, dietary or completeness claim is
made.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from culinary_copilot.domain.recommendations import (
    REASON_CONVERT_UNSUPPORTED_UNIT,
    REASON_SCALE_MISSING_SERVINGS,
    REASON_TOOL_INVALID_ARGUMENTS,
    next_action_for,
)
from culinary_copilot.recipes.llm_validate import canonical_unit
from culinary_copilot.tools.registry import ToolContext, ToolDefinition

# Base-unit factors: mass -> grams, volume -> milliliters. Count pieces
# scale but never convert. Qualitative units (pinch/dash/...) are known
# units for scaling (they multiply) but refuse conversion.
_MASS_TO_G: dict[str, float] = {
    "mg": 0.001,
    "g": 1.0,
    "kg": 1000.0,
    "oz": 28.349523125,
    "lb": 453.59237,
}

_VOLUME_TO_ML: dict[str, float] = {
    "ml": 1.0,
    "cl": 10.0,
    "l": 1000.0,
    "tsp": 4.92892159375,
    "tbsp": 14.78676478125,
    "fl_oz": 29.5735295625,
    "cup": 236.5882365,
    "pint": 473.176473,
    "quart": 946.352946,
    "gallon": 3785.411784,
}

#: Canonical units of the metric system (our conversion tables).
_METRIC_UNITS = frozenset({"mg", "g", "kg", "ml", "cl", "l"})
#: Canonical units of US customary (our conversion tables).
_US_CUSTOMARY_UNITS = frozenset(
    {"oz", "lb", "tsp", "tbsp", "fl_oz", "cup", "pint", "quart", "gallon"}
)


def _unit_system(unit: str) -> str:
    """Unit system of a canonical unit (``metric`` / ``us_customary``)."""
    if unit in _METRIC_UNITS:
        return "metric"
    return "us_customary"


def _is_qualitative(unit: str) -> bool:
    """True for known units outside mass/volume/count (pinch, dash, ...)."""
    return unit not in _MASS_TO_G and unit not in _VOLUME_TO_ML and unit != "count"


def _parse_amount(raw: Any) -> Fraction | None:
    """Parse a stored amount (rational string, int or float) to Fraction."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, int):
            return Fraction(raw, 1)
        if isinstance(raw, float):
            if not math.isfinite(raw) or raw <= 0:
                return None
            return Fraction(raw).limit_denominator(10000)
        text = str(raw).strip()
        if not text:
            return None
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return Fraction(value).limit_denominator(10000)


def servings_of(doc: dict[str, Any]) -> float | None:
    """Source servings count when known and positive, else None."""
    raw = doc.get("servings")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if math.isfinite(value) and value > 0:
            return value
        return None
    return None


class ScaleRecipeArgs(BaseModel):
    """Arguments for ``scale_recipe``."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    target_servings: float = Field(gt=0)


class ConvertUnitsArgs(BaseModel):
    """Arguments for ``convert_units``."""

    model_config = ConfigDict(extra="forbid")

    amount: float = Field(gt=0)
    from_unit: str = Field(min_length=1, max_length=50)
    to_unit: str = Field(min_length=1, max_length=50)


def _refusal(reason: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": "invalid_arguments",
        "reason": reason,
        "message": message,
        "next_action": next_action_for(reason),
    }


async def scale_recipe_impl(args: ScaleRecipeArgs, context: ToolContext) -> dict[str, Any]:
    """Scale known-unit quantities by target/source servings.

    Only ingredients with a parseable amount AND a known unit
    (``canonical_unit`` not None) are scaled. Everything else is
    returned in ``unknown_quantities`` with its original text — never
    scaled, never dropped.
    """
    from culinary_copilot.recipes.repository import SUPPORTED_DATASETS, get_recipe

    if args.dataset_id not in SUPPORTED_DATASETS:
        return _refusal(
            REASON_TOOL_INVALID_ARGUMENTS,
            f"scale_recipe: unsupported dataset_id {args.dataset_id!r}",
        )
    engine = getattr(context, "engine", None)
    if engine is None:
        from culinary_copilot.domain.recommendations import (
            REASON_TOOL_NOT_CONFIGURED,
        )
        from culinary_copilot.domain.recommendations import (
            next_action_for as _naf,
        )

        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": REASON_TOOL_NOT_CONFIGURED,
            "message": "scale_recipe not configured: no recipe engine",
            "next_action": _naf(REASON_TOOL_NOT_CONFIGURED),
        }
    import asyncio as _asyncio

    try:
        doc = await _asyncio.to_thread(
            get_recipe, engine, args.source_id, dataset_id=args.dataset_id
        )
    except ValueError as exc:
        return _refusal(REASON_TOOL_INVALID_ARGUMENTS, f"scale_recipe invalid: {exc}")
    if doc is None:
        return _refusal(
            REASON_TOOL_INVALID_ARGUMENTS,
            "scale_recipe: recipe not found for (dataset_id, source_id)",
        )
    source_servings = servings_of(doc)
    if source_servings is None:
        return _refusal(
            REASON_SCALE_MISSING_SERVINGS,
            "scale_recipe refused: source servings unknown; no factor can be derived",
        )
    if not math.isfinite(float(args.target_servings)) or float(args.target_servings) <= 0:
        return _refusal(
            REASON_TOOL_INVALID_ARGUMENTS, "scale_recipe: target_servings must be positive"
        )
    factor = Fraction(float(args.target_servings)) / Fraction(source_servings)
    scaled: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    ingredients = doc.get("ingredients")
    if not isinstance(ingredients, list):
        ingredients = []
    for item in ingredients:
        if not isinstance(item, dict):
            unknown.append({"original": None, "reason": "unparsed_ingredient"})
            continue
        amount = _parse_amount(item.get("amount", item.get("amount_text")))
        unit_raw = item.get("unit")
        unit = canonical_unit(unit_raw) if isinstance(unit_raw, str) else None
        # Bare counts ("count") are scalable known units; None/unknown stay unknown.
        if unit is None and unit_raw == "count":
            unit = "count"
        name = item.get("canonical") or item.get("name") or item.get("original")
        if amount is None or amount <= 0 or unit is None:
            unknown.append(
                {
                    "ingredient": name,
                    "quantity_text": item.get("quantity_text"),
                    "unit": unit_raw,
                    "reason": "unknown_or_unparsed_quantity",
                }
            )
            continue
        new_amount = amount * factor
        scaled.append(
            {
                "ingredient": name,
                "original_amount": str(amount),
                "scaled_amount": str(new_amount),
                "scaled_amount_float": float(new_amount),
                "unit": unit,
                "approximate": bool(_is_qualitative(unit)),
                "quantity_text": item.get("quantity_text"),
            }
        )
    return {
        "ok": True,
        "dataset_id": args.dataset_id,
        "source_id": args.source_id,
        "source_servings": source_servings,
        "target_servings": float(args.target_servings),
        "factor": float(factor),
        "scaled": scaled,
        "unknown_quantities": unknown,
    }


async def convert_units_impl(args: ConvertUnitsArgs, context: ToolContext) -> dict[str, Any]:
    """Convert ``amount`` from one unit to another within one dimension.

    Every success states ``unit_system`` (``metric`` / ``us_customary``,
    the system of the output unit). ``count`` units never convert
    (identity ``count`` -> ``count`` is the only allowed count case).
    """
    _ = context
    from_u = canonical_unit(args.from_unit)
    to_u = canonical_unit(args.to_unit)
    if from_u is None or to_u is None:
        return _refusal(
            REASON_CONVERT_UNSUPPORTED_UNIT,
            f"convert_units refused: unsupported unit {args.from_unit!r} -> {args.to_unit!r}",
        )
    if from_u == "count" or to_u == "count":
        if from_u == "count" and to_u == "count":
            return {
                "ok": True,
                "amount": float(args.amount),
                "unit": to_u,
                "unit_system": "count",
            }
        return _refusal(
            REASON_CONVERT_UNSUPPORTED_UNIT,
            f"convert_units refused: count units never convert ({from_u!r} -> {to_u!r})",
        )
    if from_u == to_u:
        return {
            "ok": True,
            "amount": float(args.amount),
            "unit": to_u,
            "unit_system": _unit_system(to_u),
        }
    if from_u in _MASS_TO_G and to_u in _MASS_TO_G:
        grams = float(args.amount) * _MASS_TO_G[from_u]
        return {
            "ok": True,
            "amount": grams / _MASS_TO_G[to_u],
            "unit": to_u,
            "unit_system": _unit_system(to_u),
        }
    if from_u in _VOLUME_TO_ML and to_u in _VOLUME_TO_ML:
        ml = float(args.amount) * _VOLUME_TO_ML[from_u]
        return {
            "ok": True,
            "amount": ml / _VOLUME_TO_ML[to_u],
            "unit": to_u,
            "unit_system": _unit_system(to_u),
        }
    return _refusal(
        REASON_CONVERT_UNSUPPORTED_UNIT,
        f"convert_units refused: cannot convert {from_u!r} -> {to_u!r} "
        "(mass/volume/count/qualitative groups do not interconvert)",
    )


def tool_definitions(timeout_s: float = 10.0) -> list[ToolDefinition]:
    """Measure-tool definitions (timeout server-set, never caller-set)."""
    return [
        ToolDefinition(
            name="scale_recipe",
            description="Scale known-unit quantities by target servings; unknown stays unknown.",
            args_model=ScaleRecipeArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="convert_units",
            description=(
                "Deterministic mass/volume conversion; refuses unknown or cross-group units."
            ),
            args_model=ConvertUnitsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
    ]


__all__ = [
    "ConvertUnitsArgs",
    "ScaleRecipeArgs",
    "convert_units_impl",
    "scale_recipe_impl",
    "tool_definitions",
]
